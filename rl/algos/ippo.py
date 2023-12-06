from typing import Callable

import jax
import jax.numpy as jnp
import ml_collections
import numpy as np

from rl.base import Base, EnvType, EnvProcs, AlgoType
from rl.buffer import OnPolicyBuffer, OnPolicyExp
from rl.timesteps import calculate_gaes_targets
from rl.modules.policy_value import TrainStatePolicyValue, ParamsPolicyValue
from rl.train import train

from rl.algos.ppo import train_state_policy_value_factory
from rl.algos.ppo import explore_factory
from rl.algos.ppo import update_step_factory

from rl.types import ParallelEnv, SubProcVecParallelEnv
from rl.types import DictArray


def process_experience_factory(
    train_state: TrainStatePolicyValue,
    config: ml_collections.ConfigDict,
    vectorized: bool,
):
    from rl.buffer import stack_experiences

    def compute_values_gaes(
        params: ParamsPolicyValue,
        observations: jax.Array,
        next_observations: jax.Array,
        dones: jax.Array,
        rewards: jax.Array,
    ):
        all_obs = jnp.concatenate([observations, next_observations[-1:]], axis=0)
        all_hiddens = train_state.encoder_fn({"params": params.params_encoder}, all_obs)
        all_values = train_state.value_fn({"params": params.params_value}, all_hiddens)

        values = all_values[:-1]
        next_values = all_values[1:]

        not_dones = 1.0 - dones[..., None]
        discounts = config.gamma * not_dones

        rewards = rewards[..., None]
        gaes, targets = calculate_gaes_targets(
            values, next_values, discounts, rewards, config._lambda, config.normalize
        )

        return gaes, targets, values

    gaes_fn = compute_values_gaes
    if vectorized:
        gaes_fn = jax.vmap(gaes_fn, in_axes=(None, 1, 1, 1, 1), out_axes=1)

    @jax.jit
    def fn(params: ParamsPolicyValue, sample: list[OnPolicyExp]):
        stacked = stack_experiences(sample)

        observations = stacked.observation
        gaes, targets, values = {}, {}, {}
        for agent in observations.keys():
            g, t, v = gaes_fn(
                params,
                observations[agent],
                stacked.next_observation[agent],
                stacked.done[agent],
                stacked.reward[agent],
            )
            gaes[agent] = g
            targets[agent] = t
            values[agent] = v

        actions = jax.tree_map(lambda x: x[..., None], stacked.action)
        log_probs = jax.tree_map(lambda x: x[..., None], stacked.log_prob)

        observations = jnp.concatenate(list(observations.values()), axis=1)
        actions = jnp.concatenate(list(actions.values()), axis=1)
        log_probs = jnp.concatenate(list(log_probs.values()), axis=1)
        gaes = jnp.concatenate(list(gaes.values()), axis=1)
        targets = jnp.concatenate(list(targets.values()), axis=1)
        values = jnp.concatenate(list(values.values()), axis=1)

        if vectorized:
            observations = jnp.reshape(observations, (-1, *observations.shape[2:]))
            actions = jnp.reshape(actions, (-1, *actions.shape[2:]))
            log_probs = jnp.reshape(log_probs, (-1, *log_probs.shape[2:]))
            gaes = jnp.reshape(gaes, (-1, *gaes.shape[2:]))
            targets = jnp.reshape(targets, (-1, *targets.shape[2:]))
            values = jnp.reshape(values, (-1, *values.shape[2:]))

        return observations, actions, log_probs, gaes, targets, values

    return fn


class PPO(Base):
    def __init__(
        self,
        seed: int,
        config: ml_collections.ConfigDict,
        *,
        rearrange_pattern: str = "b h w c -> b h w c",
        preprocess_fn: Callable = None,
        n_envs: int = 1,
        run_name: str = None,
        tabulate: bool = False,
    ):
        Base.__init__(
            self,
            seed=seed,
            config=config,
            train_state_factory=train_state_policy_value_factory,
            explore_factory=explore_factory,
            process_experience_factory=process_experience_factory,
            update_step_factory=update_step_factory,
            rearrange_pattern=rearrange_pattern,
            preprocess_fn=preprocess_fn,
            n_envs=n_envs,
            run_name=run_name,
            tabulate=tabulate,
        )

    def select_action(self, observation: DictArray) -> tuple[DictArray, DictArray]:
        return self.explore(observation)

    def explore(self, observation: DictArray) -> tuple[DictArray, DictArray]:
        def fn(params: ParamsPolicyValue, key: jax.Array, observation: DictArray):
            action, log_prob = {}, {}
            for agent, obs in observation.items():
                key, _k = jax.random.split(key)
                a, lp = self.explore_fn(params, _k, obs)
                action[agent] = a
                log_prob[agent] = lp
            return action, log_prob

        return fn(self.state.params, self.nextkey(), observation)

    def should_update(self, step: int, buffer: OnPolicyBuffer) -> None:
        return len(buffer) >= self.config.max_buffer_size

    def update(self, buffer: OnPolicyBuffer):
        def fn(state: TrainStatePolicyValue, key: jax.Array, sample: tuple):
            experiences = self.process_experience_fn(state.params, sample)

            loss = 0.0
            for epoch in range(self.config.num_epochs):
                key, _k = jax.random.split(key)
                state, l, info = self.update_step_fn(state, _k, experiences)
                loss += l

            loss /= self.config.num_epochs
            info["total_loss"] = loss
            return state, info

        sample = buffer.sample()
        self.state, info = fn(self.state, self.nextkey(), sample)
        return info

    def train(
        self,
        env: ParallelEnv | SubProcVecParallelEnv,
        n_env_steps: int,
        callbacks: list,
    ):
        return train(
            int(np.asarray(self.nextkey())[0]),
            self,
            env,
            n_env_steps,
            EnvType.PARALLEL,
            EnvProcs.ONE if self.n_envs == 1 else EnvProcs.MANY,
            AlgoType.ON_POLICY,
            saver=self.saver,
            callbacks=callbacks,
        )

    def resume(
        self,
        env: ParallelEnv | SubProcVecParallelEnv,
        n_env_steps: int,
        callbacks: list,
    ):
        step, self.state = self.saver.restore_latest_step(self.state)

        return train(
            int(np.asarray(self.nextkey())[0]),
            self,
            env,
            n_env_steps,
            EnvType.PARALLEL,
            EnvProcs.ONE if self.n_envs == 1 else EnvProcs.MANY,
            AlgoType.ON_POLICY,
            start_step=step,
            saver=self.saver,
            callbacks=callbacks,
        )
