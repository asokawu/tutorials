"""
Asynchronous Advantage Actor Critic (A3C) with continuous action space, Reinforcement Learning.

The Pendulum example. Version 1: convergence promised

View more on [莫烦Python] : https://morvanzhou.github.io/tutorials/

Using:
tensorflow 1.0
gym 0.8.0
"""

import multiprocessing
import threading
import tensorflow as tf
import numpy as np
import gym
import os
import shutil

np.random.seed(2)
tf.set_random_seed(2)  # reproducible

GAME = 'CartPole-v0'
OUTPUT_GRAPH = True
LOG_DIR = './log'
N_WORKERS = multiprocessing.cpu_count()
MAX_GLOBAL_EP = 3000
GLOBAL_NET_SCOPE = 'Global_Net'
UPDATE_GLOBAL_ITER = 5
GAMMA = 0.9
ENTROPY_BETA = 0.001
LR_A = 0.001    # learning rate for actor
LR_C = 0.001    # learning rate for critic

env = gym.make(GAME)

N_S = env.observation_space.shape[0]
N_A = env.action_space.n


class ACNet(object):
    def __init__(self, scope, n_s, n_a, sess=None,
                 opt_a=None, opt_c=None, global_a_params=None, global_c_params=None):

        if scope == GLOBAL_NET_SCOPE:   # get global network
            with tf.variable_scope(scope):
                self.s = tf.placeholder(tf.float32, [None, n_s], 'S')
                self._build_net(n_a)
                self.a_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope + '/actor')
                self.c_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope + '/critic')
        else:   # local net, calculate losses
            self.sess = sess
            with tf.variable_scope(scope):
                self.s = tf.placeholder(tf.float32, [None, n_s], 'S')
                self.a_his = tf.placeholder(tf.int32, [None, ], 'A')
                self.v_target = tf.placeholder(tf.float32, [None, 1], 'Vtarget')

                self.a_prob, self.v = self._build_net(n_a)

                td = tf.subtract(self.v_target, self.v, name='TD_error')

                with tf.name_scope('c_loss'):
                    self.c_loss = tf.reduce_sum(tf.square(td))

                with tf.name_scope('a_loss'):
                    log_prob = tf.reduce_sum(tf.log(self.a_prob) * tf.one_hot(self.a_his, n_a), axis=1)
                    exp_v = log_prob * td
                    entropy = -tf.reduce_sum(self.a_prob * tf.log(self.a_prob))  # encourage exploration
                    self.exp_v = tf.reduce_sum(ENTROPY_BETA * entropy + exp_v)
                    self.a_loss = -self.exp_v

                with tf.name_scope('local_grad'):
                    self.a_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope + '/actor')
                    self.c_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope + '/critic')
                    self.a_grads = tf.gradients(self.a_loss, self.a_params)  # get local gradients
                    self.c_grads = tf.gradients(self.c_loss, self.c_params)

            with tf.name_scope('sync'):
                with tf.name_scope('pull'):
                    self.pull_a_params_op = [l_p.assign(g_p) for l_p, g_p in zip(self.a_params, global_a_params)]
                    self.pull_c_params_op = [l_p.assign(g_p) for l_p, g_p in zip(self.c_params, global_c_params)]
                with tf.name_scope('push'):
                    self.update_a_op = opt_a.apply_gradients(zip(self.a_grads, global_a_params))
                    self.update_c_op = opt_c.apply_gradients(zip(self.c_grads, global_c_params))

    def _build_net(self, n_a):
        w_init = tf.random_normal_initializer(0., .1)
        with tf.variable_scope('actor'):
            l_a = tf.layers.dense(self.s, 100, tf.nn.relu, kernel_initializer=w_init, name='la')
            a_prob = tf.layers.dense(l_a, n_a, tf.nn.softmax, kernel_initializer=w_init, name='ap')
        with tf.variable_scope('critic'):
            l_c = tf.layers.dense(self.s, 60, tf.nn.relu, kernel_initializer=w_init, name='lc')
            v = tf.layers.dense(l_c, 1, kernel_initializer=w_init, name='v')  # state value
        return a_prob, v

    def update_global(self, feed_dict):  # run by a local
        self.sess.run([self.update_a_op, self.update_c_op], feed_dict)  # local grads applies to global net

    def pull_global(self):  # run by a local
        self.sess.run([self.pull_a_params_op, self.pull_c_params_op])

    def choose_action(self, s):  # run by a local
        prob_weights = self.sess.run(self.a_prob, feed_dict={self.s: s[np.newaxis, :]})
        action = np.random.choice(range(prob_weights.shape[1]),
                                  p=prob_weights.ravel())  # select action w.r.t the actions prob
        return action


class Worker(object):
    def __init__(self, env, name, n_s, n_a, sess, opt_a, opt_c, g_a_params, g_c_params):
        self.env = env
        self.sess = sess
        self.name = name
        self.AC = ACNet(name, n_s, n_a, sess, opt_a, opt_c, g_a_params, g_c_params)

    def work(self, update_iter, gamma, coord):
        total_step = 1
        buffer_s, buffer_a, buffer_r = [], [], []
        while not coord.should_stop() and GLOBAL_EP.eval(self.sess) < MAX_GLOBAL_EP:
            s = self.env.reset()
            ep_r = 0
            while True:
                if self.name == 'W_0':
                    self.env.render()
                a = self.AC.choose_action(s)
                s_, r, done, info = self.env.step(a)
                if done: r = -5
                ep_r += r
                buffer_s.append(s)
                buffer_a.append(a)
                buffer_r.append(r)

                if total_step % update_iter == 0 or done:   # update global and assign to local net
                    if done:
                        v_s_ = 0   # terminal
                    else:
                        v_s_ = self.sess.run(self.AC.v, {self.AC.s: s_[np.newaxis, :]})[0, 0]
                    buffer_v_target = []
                    for r in buffer_r[::-1]:    # reverse buffer r
                        v_s_ = r + gamma * v_s_
                        buffer_v_target.append(v_s_)
                    buffer_v_target.reverse()

                    buffer_s, buffer_a, buffer_v_target = np.vstack(buffer_s), np.array(buffer_a), np.vstack(buffer_v_target)
                    feed_dict = {
                        self.AC.s: buffer_s,
                        self.AC.a_his: buffer_a,
                        self.AC.v_target: buffer_v_target,
                    }
                    self.AC.update_global(feed_dict)
                    buffer_s, buffer_a, buffer_r = [], [], []
                    self.AC.pull_global()

                s = s_
                total_step += 1
                if done:
                    print(
                        self.name,
                        "Ep:", GLOBAL_EP.eval(self.sess),
                        "| Ep_r: %.2f" % ep_r,
                          )
                    sess.run(COUNT_GLOBAL_EP)
                    break

if __name__ == "__main__":
    sess = tf.Session()

    with tf.device("/cpu:0"):
        GLOBAL_EP = tf.Variable(0, dtype=tf.int32, name='global_ep', trainable=False)
        COUNT_GLOBAL_EP = tf.assign(GLOBAL_EP, tf.add(GLOBAL_EP, tf.constant(1), name='step_ep'))
        OPT_A = tf.train.RMSPropOptimizer(LR_A, name='RMSPropA')
        OPT_C = tf.train.RMSPropOptimizer(LR_C, name='RMSPropC')
        globalAC = ACNet(GLOBAL_NET_SCOPE, N_S, N_A)  # we only need its params
        workers = []
        # Create worker
        for i in range(N_WORKERS):
            i_name = 'W_%i' % i   # worker name
            workers.append(
                Worker(
                    gym.make(GAME).unwrapped, i_name, N_S, N_A, sess,
                    OPT_A, OPT_C, globalAC.a_params, globalAC.c_params
                ))

    coord = tf.train.Coordinator()
    sess.run(tf.global_variables_initializer())

    if OUTPUT_GRAPH:
        if os.path.exists(LOG_DIR):
            shutil.rmtree(LOG_DIR)
        tf.summary.FileWriter(LOG_DIR, sess.graph)

    worker_threads = []
    for worker in workers:
        job = lambda: worker.work(UPDATE_GLOBAL_ITER,  GAMMA, coord)
        t = threading.Thread(target=job)
        t.start()
        worker_threads.append(t)
    coord.join(worker_threads)

