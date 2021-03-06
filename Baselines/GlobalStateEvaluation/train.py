from __future__ import print_function

import os
import json
import time
import pickle
import argparse

import visdom
import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F

from torch.autograd import Variable

from Baselines.GlobalStateEvaluation.test import show_test_result

from data_loader.BatchEnv import BatchGlobalFeatureEnv

class StateEvaluationGRU(torch.nn.Module):
    def __init__(self, num_inputs):
        super(StateEvaluationGRU, self).__init__()
        self.linear1 = nn.Linear(num_inputs, 1024)
        self.linear2 = nn.Linear(1024, 2048)

        self.rnn1 = nn.GRUCell(input_size=2048, hidden_size=2048)
        self.rnn2 = nn.GRUCell(input_size=2048, hidden_size=512)

        self.critic_linear = nn.Linear(512, 1)

        self.h1, self.h2 = None, None

    def forward(self, states, require_init):
        batch = states.size(1)
        if self.h1 is None or self.h2 is None:
            self.h1 = Variable(states.data.new().resize_((batch, 2048)).zero_())
            self.h2 = Variable(states.data.new().resize_((batch, 512)).zero_())
        elif True in require_init:
            h1, h2 = self.h1.data, self.h2.data
            for idx, init in enumerate(require_init):
                if init:
                    h1[idx].zero_()
                    h2[idx].zero_()
            self.h1, self.h2 = Variable(h1), Variable(h2)
        else:
            pass

        values = []
        for idx, state in enumerate(states):
            x = F.relu(self.linear1(state))
            x = F.relu(self.linear2(x))
            self.h1 = self.rnn1(x, self.h1)
            self.h2 = self.rnn2(self.h1, self.h2)

            values.append(F.sigmoid(self.critic_linear(self.h2)))

        return values

    def detach(self):
        if self.h1 is not None:
            self.h1.detach_()
        if self.h2 is not None:
            self.h2.detach_()

def train(model, env, args):
    #################################### PLOT ###################################################
    STEPS = 10
    LAMBDA = 0.99
    vis = visdom.Visdom(env=args.name+'[{}]'.format(args.phrase))
    pre_per_replay = [[] for _ in range(args.n_replays)]
    gt_per_replay = [[] for _ in range(args.n_replays)]
    acc = None
    win = vis.line(X=np.zeros(1), Y=np.zeros(1))
    loss_win = vis.line(X=np.zeros(1), Y=np.zeros(1))

    #################################### TRAIN ######################################################
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)

    gpu_id = args.gpu_id
    with torch.cuda.device(gpu_id):
        model = model.cuda() if gpu_id >= 0 else model
    model.train()
    optimizer = optim.Adam(model.parameters(), lr=args.lr)

    epoch = 0
    save = args.save_intervel
    env_return = env.step()
    if env_return is not None:
        (states, rewards), require_init = env_return
    with torch.cuda.device(gpu_id):
        states = torch.from_numpy(states).float()
        rewards = torch.from_numpy(rewards).float()
        if gpu_id >= 0:
            states = states.cuda()
            rewards = rewards.cuda()

    while True:
        values = model(Variable(states), require_init)

        value_loss = 0
        for value, reward in zip(values, rewards):
            value_loss = value_loss + F.binary_cross_entropy(value, Variable(reward))

        model.zero_grad()
        value_loss.backward()
        optimizer.step()
        model.detach()

        if env.epoch > epoch:
            epoch = env.epoch
            for p in optimizer.param_groups:
                p['lr'] *= 0.1

        ############################ PLOT ##########################################
        vis.updateTrace(X=np.asarray([env.step_count()]),
                        Y=np.asarray(value_loss.data.cpu().numpy()),
                        win=loss_win,
                        name='value')

        values_np = np.swapaxes(np.asarray([value.data.cpu().numpy() for value in values]), 0, 1)
        rewards_np = np.swapaxes(rewards.cpu().numpy(), 0, 1)

        for idx, (value, reward, init) in enumerate(zip(values_np, rewards_np, require_init)):
            if init and len(pre_per_replay[idx]) > 0:
                pre_per_replay[idx] = np.asarray(pre_per_replay[idx], dtype=np.uint8)
                gt_per_replay[idx] = np.asarray(gt_per_replay[idx], dtype=np.uint8)

                step = len(pre_per_replay[idx]) // STEPS
                if step > 0:
                    acc_tmp = []
                    for s in range(STEPS):
                        value_pre = pre_per_replay[idx][s*step:(s+1)*step]
                        value_gt = gt_per_replay[idx][s*step:(s+1)*step]
                        acc_tmp.append(np.mean(value_pre == value_gt))

                    acc_tmp = np.asarray(acc_tmp)
                    if acc is None:
                        acc = acc_tmp
                    else:
                        acc = LAMBDA * acc + (1-LAMBDA) * acc_tmp

                    if acc is None:
                        continue
                    for s in range(STEPS):
                        vis.updateTrace(X=np.asarray([env.step_count()]),
                                        Y=np.asarray([acc[s]]),
                                        win=win,
                                        name='{}[{}%~{}%]'.format('value', s*10, (s+1)*10))
                    vis.updateTrace(X=np.asarray([env.step_count()]),
                                    Y=np.asarray([np.mean(acc)]),
                                    win=win,
                                    name='value[TOTAL]')

                pre_per_replay[idx] = []
                gt_per_replay[idx] = []

            pre_per_replay[idx].append(int(value[-1] >= 0.5))
            gt_per_replay[idx].append(int(reward[-1]))

        ####################### NEXT BATCH ###################################
        env_return = env.step()
        if env_return is not None:
            (raw_states, raw_rewards), require_init = env_return
            states = states.copy_(torch.from_numpy(raw_states).float())
            rewards = rewards.copy_(torch.from_numpy(raw_rewards).float())

        if env.step_count() > save or env_return is None:
            save = env.step_count()+args.save_intervel
            torch.save(model.state_dict(),
                       os.path.join(args.model_path, 'model_iter_{}.pth'.format(env.step_count())))
            torch.save(model.state_dict(), os.path.join(args.model_path, 'model_latest.pth'))
        if env_return is None:
            env.close()
            break

def test(model, env, args):
    ######################### SAVE RESULT ############################
    value_pre_per_replay = [[]]
    value_gt_per_replay = [[]]

    ######################### TEST ###################################
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)

    gpu_id = args.gpu_id
    with torch.cuda.device(gpu_id):
        model = model.cuda() if gpu_id >= 0 else model
    model.eval()

    env_return = env.step()
    if env_return is not  None:
        (states, rewards), require_init = env_return
    with torch.cuda.device(gpu_id):
        states = torch.from_numpy(states).float()
        rewards = torch.from_numpy(rewards).float()
        if gpu_id >= 0:
            states = states.cuda()
            rewards = rewards.cuda()

    while True:
        values = model(Variable(states), require_init)
        ############################ PLOT ##########################################
        values_np = np.squeeze(np.vstack([value.data.cpu().numpy() for value in values]))
        rewards_np = np.squeeze(rewards.cpu().numpy())

        if require_init[-1] and len(value_gt_per_replay[-1]) > 0:
            value_pre_per_replay[-1] = np.ravel(np.hstack(value_pre_per_replay[-1]))
            value_gt_per_replay[-1] = np.ravel(np.hstack(value_gt_per_replay[-1]))

            value_pre_per_replay.append([])
            value_gt_per_replay.append([])

        value_pre_per_replay[-1].append(values_np>=0.5)
        value_gt_per_replay[-1].append(rewards_np)

        ########################### NEXT BATCH #############################################
        env_return = env.step()
        if env_return is not None:
            (raw_states, raw_rewards), require_init = env_return
            states = states.copy_(torch.from_numpy(raw_states).float())
            rewards = rewards.copy_(torch.from_numpy(raw_rewards).float())
        else:
            value_pre_per_replay[-1] = np.ravel(np.hstack(value_pre_per_replay[-1]))
            value_gt_per_replay[-1] = np.ravel(np.hstack(value_gt_per_replay[-1]))

            env.close()
            break

    return value_pre_per_replay, value_gt_per_replay

def next_path(model_folder, paths):
    models = {int(os.path.basename(model).split('.')[0].split('_')[-1])
                for model in os.listdir(model_folder) if 'latest' not in model}
    models_not_process = models - paths
    if len(models_not_process) == 0:
        return None
    models_not_process = sorted(models_not_process)
    paths.add(models_not_process[0])

    return os.path.join(model_folder, 'model_iter_{}.pth'.format(models_not_process[0]))

def main():
    # Training settings
    parser = argparse.ArgumentParser(description='Global State Evaluation : StarCraft II')
    parser.add_argument('--name', type=str, default='StarCraft II:TvT',
                        help='Experiment name. All outputs will be stored in checkpoints/[name]/')
    parser.add_argument('--replays_path', default='train_val_test/Terran_vs_Terran',
                        help='Path for training, validation and test set (default: train_val_test/Terran_vs_Terran)')
    parser.add_argument('--race', default='Terran', help='Which race? (default: Terran)')
    parser.add_argument('--enemy_race', default='Terran', help='Which the enemy race? (default: Terran)')
    parser.add_argument('--phrase', type=str, default='train',
                        help='train|val|test (default: train)')
    parser.add_argument('--gpu_id', default=0, type=int, help='Which GPU to use [-1 indicate CPU] (default: 0)')

    parser.add_argument('--lr', type=float, default=0.001, help='Learning rate (default: 0.001)')
    parser.add_argument('--seed', type=int, default=1, help='Random seed (default: 1)')

    parser.add_argument('--n_steps', type=int, default=20, help='# of forward steps (default: 20)')
    parser.add_argument('--n_replays', type=int, default=256, help='# of replays (default: 256)')
    parser.add_argument('--n_epoch', type=int, default=10, help='# of epoches (default: 10)')

    parser.add_argument('--save_intervel', type=int, default=1000000,
                        help='Frequency of model saving (default: 1000000)')
    args = parser.parse_args()

    args.save_path = os.path.join('checkpoints', args.name)
    args.model_path = os.path.join(args.save_path, 'snapshots')

    print('------------ Options -------------')
    for k, v in sorted(vars(args).items()):
        print('{}: {}'.format(k, v))
    print('-------------- End ----------------')

    if args.phrase == 'train':
        if not os.path.isdir(args.save_path):
            os.makedirs(args.save_path)
        if not os.path.isdir(args.model_path):
            os.makedirs(args.model_path)
        with open(os.path.join(args.save_path, 'config'), 'w') as f:
            f.write(json.dumps(vars(args)))

        env = BatchGlobalFeatureEnv()
        env.init(os.path.join(args.replays_path, '{}.json'.format(args.phrase)),
                    './', args.race, args.enemy_race, n_steps=args.n_steps, seed=args.seed,
                        n_replays=args.n_replays, epochs=args.n_epoch)
        model = StateEvaluationGRU(env.n_features)
        train(model, env, args)
    elif 'val' in args.phrase or 'test' in args.phrase:
        test_result_path = os.path.join(args.save_path, args.phrase)
        if not os.path.isdir(test_result_path):
            os.makedirs(test_result_path)

        dataset_path = 'test.json' if 'test' in args.phrase else 'val.json'
        paths = set()
        while True:
            path = next_path(args.model_path, paths)
            if path is not None:
                print('[{}]Testing {} ...'.format(len(paths), path))

                env = BatchGlobalFeatureEnv()
                env.init(os.path.join(args.replays_path, dataset_path),
                            './', args.race, args.enemy_race, n_steps=args.n_steps,
                                            seed=args.seed, n_replays=1, epochs=1)
                model = StateEvaluationGRU(env.n_features)
                model.load_state_dict(torch.load(path))
                result = test(model, env, args)
                with open(os.path.join(test_result_path, os.path.basename(path)), 'wb') as f:
                    pickle.dump(result, f)
                show_test_result(args.name, args.phrase, result, title=len(paths)-1)
            else:
                time.sleep(60)

if __name__ == '__main__':
    main()