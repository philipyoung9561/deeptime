# This file is part of PyEMMA.
#
# Copyright (c) 2015, 2014 Computational Molecular Biology Group, Freie Universitaet Berlin (GER)
#
# PyEMMA is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
from typing import Optional, Union, List

import numpy as np
from msmtools.analysis import is_connected
from msmtools.dtraj import number_of_states
from msmtools.estimation import sample_tmatrix, transition_matrix

from sktime.base import Estimator
from sktime.markovprocess import MarkovStateModel
from sktime.markovprocess._base import BayesianPosterior
from sktime.markovprocess._transition_matrix import stationary_distribution
from sktime.markovprocess.bhmm import discrete_hmm, bayesian_hmm
from sktime.markovprocess.hmm import HiddenMarkovStateModel, MaximumLikelihoodHMSM
from sktime.markovprocess.hmm.output_model import DiscreteOutputModel
from sktime.markovprocess.util import compute_dtrajs_effective
from sktime.util import ensure_dtraj_list
import sktime.markovprocess.hmm._hmm_bindings as _bindings

__author__ = 'noe'

__all__ = [
    'BayesianHMMPosterior',
    'BayesianHMSM',
]


class BayesianHMMPosterior(BayesianPosterior):
    r""" Bayesian Hidden Markov model with samples of posterior and prior. """

    def __init__(self,
                 prior: Optional[HiddenMarkovStateModel] = None,
                 samples: Optional[List[HiddenMarkovStateModel]] = (),
                 hidden_state_trajs: Optional[List[np.ndarray]] = ()):
        super(BayesianHMMPosterior, self).__init__(prior=prior, samples=samples)
        self.hidden_state_trajectories_samples = hidden_state_trajs

    def submodel_largest(self, strong=True, connectivity_threshold='1/n', observe_nonempty=True, dtrajs=None):
        dtrajs = ensure_dtraj_list(dtrajs)
        states = self.prior.states_largest(directed=strong, connectivity_threshold=connectivity_threshold)
        obs = self.prior.nonempty_obs(dtrajs) if observe_nonempty else None
        return self.submodel(states=states, obs=obs)

    def submodel_populous(self, strong=True, connectivity_threshold='1/n', observe_nonempty=True, dtrajs=None):
        dtrajs = ensure_dtraj_list(dtrajs)
        states = self.prior.states_populous(strong=strong, connectivity_threshold=connectivity_threshold)
        obs = self.prior.nonempty_obs(dtrajs) if observe_nonempty else None
        return self.submodel(states=states, obs=obs)

    def submodel(self, states=None, obs=None):
        # restrict prior
        sub_model = self.prior.submodel(states=states, obs=obs)
        # restrict reduce samples
        subsamples = [sample.submodel(states=states, obs=obs)
                      for sample in self]
        return BayesianHMMPosterior(sub_model, subsamples, self.hidden_state_trajectories_samples)


class BayesianHMSM(Estimator):

    def __init__(self, initial_hmm: HiddenMarkovStateModel,
                 n_samples: int = 100,
                 n_transition_matrix_sampling_steps: int = 1000,
                 stride: Union[str, int] = 'effective',
                 initial_distribution_prior: Optional[Union[str, float, np.ndarray]] = 'mixed',
                 transition_matrix_prior: Union[str, np.ndarray] = 'mixed',
                 store_hidden: bool = False,
                 reversible: bool = True,
                 stationary: bool = False):
        super().__init__()
        self.initial_hmm = initial_hmm
        self.n_samples = n_samples
        self.n_transition_matrix_sampling_steps = n_transition_matrix_sampling_steps
        self.stride = stride
        self.initial_distribution_prior = initial_distribution_prior
        self.transition_matrix_prior = transition_matrix_prior
        self.store_hidden = store_hidden
        self.reversible = reversible
        self.stationary = stationary

    @property
    def stationary(self):
        return self._stationary

    @stationary.setter
    def stationary(self, value):
        self._stationary = value

    @property
    def reversible(self):
        return self._reversible

    @reversible.setter
    def reversible(self, value):
        self._reversible = value

    @property
    def store_hidden(self):
        return self._store_hidden

    @store_hidden.setter
    def store_hidden(self, value):
        self._store_hidden = value

    @property
    def transition_matrix_prior(self):
        return self._transition_matrix_prior

    @transition_matrix_prior.setter
    def transition_matrix_prior(self, value):
        self._transition_matrix_prior = value

    @property
    def initial_distribution_prior(self) -> Optional[Union[str, float, np.ndarray]]:
        return self._initial_distribution_prior

    @initial_distribution_prior.setter
    def initial_distribution_prior(self, value: Optional[Union[str, float, np.ndarray]]):
        self._initial_distribution_prior = value

    @property
    def initial_hmm(self) -> HiddenMarkovStateModel:
        return self._initial_hmm

    @initial_hmm.setter
    def initial_hmm(self, value: HiddenMarkovStateModel):
        if not isinstance(value, HiddenMarkovStateModel):
            raise ValueError(f"Initial hmm must be of type HiddenMarkovModel, but was {value.__class__.__name__}.")
        self._initial_hmm = value

    @property
    def n_samples(self) -> int:
        return self._n_samples

    @n_samples.setter
    def n_samples(self, value: int):
        self._n_samples = value

    def fetch_model(self) -> BayesianHMMPosterior:
        return self._model

    @property
    def _initial_distribution_prior_np(self) -> np.ndarray:
        if self.initial_distribution_prior is None or self.initial_distribution_prior == 'sparse':
            prior = np.zeros(self.initial_hmm.n_hidden_states)
        elif isinstance(self.initial_distribution_prior, np.ndarray):
            if self.initial_distribution_prior.ndim == 1 \
                    and len(self.initial_distribution_prior) == self.initial_hmm.n_hidden_states:
                prior = np.asarray(self.initial_distribution_prior)
            else:
                raise ValueError(f"If the initial distribution prior is given as a np array, it must be 1-dimensional "
                                 f"and be as long as there are hidden states. "
                                 f"(ndim={self.initial_distribution_prior.ndim}, "
                                 f"len={len(self.initial_distribution_prior)})")
        elif self.initial_distribution_prior == 'mixed':
            prior = self.initial_hmm.initial_distribution
        elif self.initial_distribution_prior == 'uniform':
            prior = np.ones(self.initial_hmm.n_hidden_states)
        else:
            raise ValueError(f'Initial distribution prior mode undefined: {self.initial_distribution_prior}')
        return prior

    @property
    def _transition_matrix_prior_np(self) -> np.ndarray:
        n_states = self.initial_hmm.n_hidden_states
        if self.transition_matrix_prior is None or self.transition_matrix_prior == 'sparse':
            prior = np.zeros((n_states, n_states))
        elif isinstance(self.transition_matrix_prior, np.ndarray):
            if np.array_equal(self.transition_matrix_prior.shape, (n_states, n_states)):
                prior = np.asarray(self.transition_matrix_prior)
            else:
                raise ValueError(f"If the initial distribution prior is given as a np array, it must be 2-dimensional "
                                 f"and a (n_hidden_states x n_hidden_states) matrix. "
                                 f"(ndim={self.transition_matrix_prior.ndim}, "
                                 f"shape={self.transition_matrix_prior.shape})")
        elif self.transition_matrix_prior == 'mixed':
            prior = np.copy(self.initial_hmm.transition_model.transition_matrix)
        elif self.transition_matrix_prior == 'uniform':
            prior = np.ones((n_states, n_states))
        else:
            raise ValueError(f'Initial distribution prior mode undefined: {self.transition_matrix_prior}')
        return prior

    def _update(self, model: HiddenMarkovStateModel, observations):
        """Update the current model using one round of Gibbs sampling."""
        self._update_hidden_state_trajectories(model, observations)
        self._update_emission_probabilities(model, observations)
        self._update_transition_matrix(model)

    def _update_hidden_state_trajectories(self, model: HiddenMarkovStateModel, observations):
        """Sample a new set of state trajectories from the conditional distribution P(S | T, E, O)"""
        model._hidden_state_trajectories = [
            self._sample_hidden_state_trajectory(model, obs)
            for obs in observations
        ]

    def _sample_hidden_state_trajectory(self, model: HiddenMarkovStateModel, obs):
        """Sample a hidden state trajectory from the conditional distribution P(s | T, E, o)

        Parameters
        ----------
        o_t : numpy.array with dimensions (T,)
            observation[n] is the nth observation

        Returns
        -------
        s_t : numpy.array with dimensions (T,) of type `dtype`
            Hidden state trajectory, with s_t[t] the hidden state corresponding to observation o_t[t]
        """

        # Determine observation trajectory length
        T = obs.shape[0]

        # Convenience access.
        A = model.transition_model.transition_matrix
        pi = model.initial_distribution

        # compute output probability matrix
        self._pobs = model.output_model.to_state_probability_trajectory(obs)
        # compute forward variables
        _bindings.util.forward(A, self._pobs, pi, T=T, alpha=self._alpha)
        # sample path
        S = _bindings.util.sample_path(self._alpha, A, self._pobs, T=T)

        return S

    def _update_emission_probabilities(self, model: HiddenMarkovStateModel, observations):
        """Sample a new set of emission probabilites from the conditional distribution P(E | S, O) """
        observations_by_state = [model.collect_observations_in_state(observations, state)
                                 for state in range(model.n_hidden_states)]
        model.output_model.sample(observations_by_state)

    def _update_transition_matrix(self, model: HiddenMarkovStateModel):
        """ Updates the hidden-state transition matrix and the initial distribution """
        C = model.transition_model.count_model.count_matrix + self.prior_C  # posterior count matrix

        # check if we work with these options
        if self.reversible and not is_connected(C, directed=True):
            raise NotImplementedError('Encountered disconnected count matrix with sampling option reversible:\n '
                                      f'{C}\nUse prior to ensure connectivity or use reversible=False.')
        # ensure consistent sparsity pattern (P0 might have additional zeros because of underflows)
        # TODO: these steps work around a bug in msmtools. Should be fixed there
        P0 = transition_matrix(C, reversible=self.reversible, maxiter=10000, warn_not_converged=False)
        zeros = np.where(P0 + P0.T == 0)
        C[zeros] = 0
        # run sampler
        Tij = sample_tmatrix(C, nsample=1, nsteps=self.transition_matrix_sampling_steps,
                             reversible=self.reversible)

        # INITIAL DISTRIBUTION
        if self.stationary:  # p0 is consistent with P
            p0 = stationary_distribution(Tij, C=C)
        else:
            n0 = model.initial_count.astype(float)
            first_timestep_counts_with_prior = n0 + self.prior_n0
            positive = first_timestep_counts_with_prior > 0
            p0 = np.zeros_like(n0)
            p0[positive] = np.random.dirichlet(first_timestep_counts_with_prior[positive])  # sample p0 from posterior

        # update HMM with new sample
        model.transition_model.update_transition_matrix(Tij)
        model.transition_model.update_stationary_distribution(p0)

    def fit(self, data, n_burn_in: int = 0, n_thin: int = 0, **kwargs):
        dtrajs = ensure_dtraj_list(data)

        # fetch priors
        p0_prior = self._initial_distribution_prior_np
        transition_matrix_prior = self._transition_matrix_prior_np
        transition_matrix = self.initial_hmm.transition_model.transition_matrix

        model = BayesianHMMPosterior()
        # update HMM Model
        model.prior = self.initial_hmm.copy()

        prior = model.prior
        prior_count_model = prior.count_model
        # check if we have a valid initial model (todo: do we need this?)
        # if self.reversible and not is_connected(prior_count_model.count_matrix):
        #     raise NotImplementedError(f'Encountered disconnected count matrix:\n{prior_count_model.count_matrix} '
        #                               f'with reversible Bayesian HMM sampler using lag={self.initial_hmm.lagtime}'
        #                               f' and stride={self.stride}. Consider using shorter lag, '
        #                               'or shorter stride (to use more of the data), '
        #                               'or using a lower value for mincount_connectivity.')
        # check if we work with these options
        if self.reversible and not is_connected(transition_matrix + transition_matrix_prior, directed=True):
            raise NotImplementedError('Trying to sample disconnected HMM with option reversible:\n '
                                      f'{transition_matrix}\n Use prior to connect, select connected subset, '
                                      f'or use reversible=False.')


        # EVALUATE STRIDE
        init_stride = self.initial_hmm.stride
        if self.stride == 'effective':
            from sktime.markovprocess.util import compute_effective_stride
            self.stride = compute_effective_stride(dtrajs, self.lagtime, self.n_states)

        # if stride is different to init_hmsm, check if microstates in lagged-strided trajs are compatible
        dtrajs_lagged_strided = compute_dtrajs_effective(
            dtrajs, lagtime=self.initial_hmm.lagtime, n_states=self.initial_hmm.n_hidden_states, stride=self.stride
        )
        if self.stride != init_stride:
            symbols = np.unique(np.concatenate(dtrajs_lagged_strided))
            if not len(np.intersect1d(self.initial_hmm.observation_symbols, symbols)) == len(symbols):
                raise ValueError('Choice of stride has excluded a different set of microstates than in '
                                 'init_hmsm. Set of observed microstates in time-lagged strided trajectories '
                                 'must match to the one used for init_hmsm estimation.')



        # here we blow up the output matrix (if needed) to the FULL state space because we want to use dtrajs in the
        # Bayesian HMM sampler. This is just an initialization.
        n_states_full = number_of_states(dtrajs)

        if prior.n_observation_states < n_states_full:
            eps = 0.01 / n_states_full  # default output probability, in order to avoid zero columns
            # full state space output matrix. make sure there are no zero columns
            full_obs_probabilities = eps * np.ones((self.n_states, n_states_full), dtype=np.float64)
            # fill active states
            full_obs_probabilities[:, prior.observation_symbols] = np.maximum(eps, prior.observation_probabilities)
            # renormalize B to make it row-stochastic
            full_obs_probabilities /= full_obs_probabilities.sum(axis=1)[:, None]
        else:
            full_obs_probabilities = prior.observation_probabilities

        maxT = np.max(len(o) for o in dtrajs)

        # pre-construct hidden variables
        temp_alpha = np.zeros((maxT, prior.n_hidden_states))
        temp_pobs = np.zeros((maxT, prior.n_hidden_states))

        try:
            # sample model is copy of prior
            sample_model = HiddenMarkovStateModel(prior.transition_model.copy(),
                                                  output_model=DiscreteOutputModel(full_obs_probabilities),
                                                  initial_distribution=prior.initial_distribution)
            # Run burn-in.
            for _ in range(n_burn_in):
                self._update(sample_model, dtrajs)

            # Collect data.
            models = []
            for _ in range(self.n_samples):
                # Run a number of Gibbs sampling updates to generate each sample.
                for _ in range(n_thin):
                    self._update(sample_model, dtrajs)
                # Save a copy of the current model.
                model_copy = sample_model.copy()
                # the viterbi path is discarded, but is needed to get a new transition matrix for each model.
                if not self.store_hidden:
                    model_copy.hidden_state_trajectory = None
                models.append(model_copy)

            model.samples = models
        finally:
            del temp_alpha
            del temp_pobs

        # repackage samples as HMSM objects and re-normalize after restricting to observable set
        if not model.prior.n_observation_states == len(model.prior.observation_symbols_full):
            for sample in model.samples:
                sample.submodel(states=None, obs=model.prior)
        samples = []
        for sample in model.samples:  # restrict to observable set if necessary
            P = sample.transition_model.transition_matrix
            pi = sample.transition_model.stationary_distribution
            init_dist = sample.initial_distribution

            pobs = sample.output_model.output_probabilities
            Bobs = pobs[:, prior.observation_symbols]
            pobs = Bobs / Bobs.sum(axis=1)[:, None]  # renormalize
            transition_model = MarkovStateModel(P, stationary_distribution=pi, count_model=prior_count_model,
                                                reversible=self.reversible)
            samples.append(HiddenMarkovStateModel(transition_model, pobs, initial_count=sample.initial_count,
                                                  initial_distribution=init_dist))

        # set new model
        self._model = model

        return self




class BayesianHMSM2(Estimator):
    r""" Estimator for a Bayesian Hidden Markov state model """

    def __init__(self, init_hmsm: HiddenMarkovStateModel,
                 n_states: int = 2,
                 lagtime: int = 1, n_samples: int = 100,
                 stride: Union[str, int] = 'effective',
                 p0_prior: Optional[Union[str, float, np.ndarray]] = 'mixed',
                 transition_matrix_prior: Union[str, np.ndarray] = 'mixed',
                 store_hidden: bool = False,
                 reversible: bool = True,
                 stationary: bool = False):
        r"""
        Estimator for a Bayesian Hidden Markov state model.

        Parameters
        ----------
        init_hmsm : :class:`HMSM <sktime.markovprocess.hidden_markov_model.HMSM>`
            Single-point estimate of HMSM object around which errors will be evaluated.
            There is a static method available that can be used to generate a default prior.
        n_states : int, optional, default=2
            number of hidden states
        lagtime : int, optional, default=1
            lagtime to estimate the HMSM at
        n_samples : int, optional, default=100
            Number of Gibbs sampling steps
        stride : str or int, default='effective'
            stride between two lagged trajectories extracted from the input
            trajectories. Given trajectory s[t], stride and lag will result
            in trajectories
                s[0], s[tau], s[2 tau], ...
                s[stride], s[stride + tau], s[stride + 2 tau], ...
            Setting stride = 1 will result in using all data (useful for
            maximum likelihood estimator), while a Bayesian estimator requires
            a longer stride in order to have statistically uncorrelated
            trajectories. Setting stride = 'effective' uses the largest
            neglected timescale as an estimate for the correlation time and
            sets the stride accordingly.
        p0_prior : None, str, float or ndarray(n)
            Prior for the initial distribution of the HMM. Will only be active
            if stationary=False (stationary=True means that p0 is identical to
            the stationary distribution of the transition matrix).
            Currently implements different versions of the Dirichlet prior that
            is conjugate to the Dirichlet distribution of p0. p0 is sampled from:

            .. math:
                p0 \sim \prod_i (p0)_i^{a_i + n_i - 1}
            where :math:`n_i` are the number of times a hidden trajectory was in
            state :math:`i` at time step 0 and :math:`a_i` is the prior count.
            Following options are available:

            * 'mixed' (default),  :math:`a_i = p_{0,init}`, where :math:`p_{0,init}`
              is the initial distribution of initial_model.
            *  ndarray(n) or float,
               the given array will be used as A.
            *  'uniform',  :math:`a_i = 1`
            *   None,  :math:`a_i = 0`. This option ensures coincidence between
                sample mean an MLE. Will sooner or later lead to sampling problems,
                because as soon as zero trajectories are drawn from a given state,
                the sampler cannot recover and that state will never serve as a starting
                state subsequently. Only recommended in the large data regime and
                when the probability to sample zero trajectories from any state
                is negligible.
        transition_matrix_prior : str or ndarray(n, n)
            Prior for the HMM transition matrix.
            Currently implements Dirichlet priors if reversible=False and reversible
            transition matrix priors as described in [3]_ if reversible=True. For the
            nonreversible case the posterior of transition matrix :math:`P` is:

            .. math:
                P \sim \prod_{i,j} p_{ij}^{b_{ij} + c_{ij} - 1}
            where :math:`c_{ij}` are the number of transitions found for hidden
            trajectories and :math:`b_{ij}` are prior counts.

            * 'mixed' (default),  :math:`b_{ij} = p_{ij,init}`, where :math:`p_{ij,init}`
              is the transition matrix of initial_model. That means one prior
              count will be used per row.
            * ndarray(n, n) or broadcastable,
              the given array will be used as B.
            * 'uniform',  :math:`b_{ij} = 1`
            * None,  :math:`b_ij = 0`. This option ensures coincidence between
              sample mean an MLE. Will sooner or later lead to sampling problems,
              because as soon as a transition :math:`ij` will not occur in a
              sample, the sampler cannot recover and that transition will never
              be sampled again. This option is not recommended unless you have
              a small HMM and a lot of data.
        store_hidden : bool, optional, default=False
            store hidden trajectories in sampled HMMs
        reversible : bool, optional, default=True
            If True, a prior that enforces reversible transition matrices (detailed balance) is used;
            otherwise, a standard  non-reversible prior is used.
        stationary : bool, optional, default=False
            If True, the stationary distribution of the transition matrix will be used as initial distribution.
            Only use True if you are confident that the observation trajectories are started from a global
            equilibrium. If False, the initial distribution will be estimated as usual from the first step
            of the hidden trajectories.

        References
        ----------
        .. [1] F. Noe, H. Wu, J.-H. Prinz and N. Plattner: Projected and hidden
            Markov models for calculating kinetics and metastable states of complex
            molecules. J. Chem. Phys. 139, 184114 (2013)
        .. [2] J. D. Chodera Et Al: Bayesian hidden Markov model analysis of
            single-molecule force spectroscopy: Characterizing kinetics under
            measurement uncertainty. arXiv:1108.1430 (2011)
        .. [3] Trendelkamp-Schroer, B., H. Wu, F. Paul and F. Noe:
            Estimation and uncertainty of reversible Markov models.
            J. Chem. Phys. 143, 174101 (2015).
        """

        super(BayesianHMSM, self).__init__()
        self.n_states = n_states
        self.lagtime = lagtime
        self.stride = stride

        self.p0_prior = p0_prior
        self.transition_matrix_prior = transition_matrix_prior
        self.init_hmsm = init_hmsm
        self.store_hidden = store_hidden
        self.reversible = reversible
        self.stationary = stationary
        self.n_samples = n_samples

    def fetch_model(self) -> BayesianHMMPosterior:
        return self._model

    @property
    def init_hmsm(self):
        return self._init_hmsm

    @init_hmsm.setter
    def init_hmsm(self, value: Optional[HiddenMarkovStateModel]):
        if value is not None and not issubclass(value.__class__, HiddenMarkovStateModel):
            raise ValueError('hmsm must be of type HMSM')
        self._init_hmsm = value

    @staticmethod
    def default_prior_estimator(n_states: int, lagtime: int, stride: Union[str, int] = 'effective',
                                reversible: bool = True, stationary: bool = False,
                                separate: Optional[Union[int, List[int]]] = None, dt_traj: str = '1 step'):
        accuracy = 1e-2  # sufficient accuracy for an initial guess
        prior_estimator = MaximumLikelihoodHMSM(
            n_states=n_states, lagtime=lagtime, stride=stride,
            reversible=reversible, stationary=stationary, physical_time=dt_traj,
            separate=separate, connectivity=None,
            accuracy=accuracy, observe_nonempty=False
        )
        return prior_estimator

    @staticmethod
    def default(dtrajs, n_states: int, lagtime: int, n_samples: int = 100,
                stride: Union[str, int] = 'effective',
                p0_prior: Optional[Union[str, float, np.ndarray]] = 'mixed',
                transition_matrix_prior: Union[str, np.ndarray] = 'mixed',
                separate: Optional[Union[int, List[int]]] = None,
                store_hidden: bool = False,
                reversible: bool = True,
                stationary: bool = False,
                dt_traj: str = '1 step'):
        """
        Computes a default prior for a BHMSM and uses that for error estimation.
        For a more detailed description of the arguments please
        refer to :class:`HMSM <sktime.markovprocess.hidden_markov_model.HMSM>` or
        :class:`BayesianHMSM <sktime.markovprocess.bayesian_hmsm.BayesianHMSM>`.
        """
        dtrajs = ensure_dtraj_list(dtrajs)
        prior_est = BayesianHMSM.default_prior_estimator(n_states=n_states, lagtime=lagtime, stride=stride,
                                                         reversible=reversible, stationary=stationary,
                                                         separate=separate, dt_traj=dt_traj)
        prior = prior_est.fit(dtrajs).fetch_model().submodel_largest(connectivity_threshold='1/n', dtrajs=dtrajs)

        estimator = BayesianHMSM(init_hmsm=prior, n_states=n_states, lagtime=lagtime, n_samples=n_samples,
                                 stride=stride, initial_distribution_prior=p0_prior, transition_matrix_prior=transition_matrix_prior,
                                 store_hidden=store_hidden, reversible=reversible,
                                 stationary=stationary)
        return estimator

    def fit(self, dtrajs, callback=None):
        dtrajs = ensure_dtraj_list(dtrajs)

        model = BayesianHMMPosterior()

        # check if n_states and lag are compatible
        if self.lagtime != self.init_hmsm.lagtime:
            raise ValueError('BayesianHMSM cannot be initialized with init_hmsm with incompatible lagtime.')
        if self.n_states != self.init_hmsm.n_states:
            raise ValueError('BayesianHMSM cannot be initialized with init_hmsm with incompatible n_states.')

        # EVALUATE STRIDE
        init_stride = self.init_hmsm.stride
        if self.stride == 'effective':
            from sktime.markovprocess.util import compute_effective_stride
            self.stride = compute_effective_stride(dtrajs, self.lagtime, self.n_states)

        # if stride is different to init_hmsm, check if microstates in lagged-strided trajs are compatible
        dtrajs_lagged_strided = compute_dtrajs_effective(
            dtrajs, lagtime=self.lagtime, n_states=self.n_states, stride=self.stride
        )
        if self.stride != init_stride:
            symbols = np.unique(np.concatenate(dtrajs_lagged_strided))
            if not np.all(self.init_hmsm.observation_state_symbols == symbols):
                raise ValueError('Choice of stride has excluded a different set of microstates than in '
                                 'init_hmsm. Set of observed microstates in time-lagged strided trajectories '
                                 'must match to the one used for init_hmsm estimation.')

        # update HMM Model
        model.prior = self.init_hmsm.copy()

        prior = model.prior
        prior_count_model = prior.count_model
        # check if we have a valid initial model
        if self.reversible and not is_connected(prior_count_model.count_matrix):
            raise NotImplementedError(f'Encountered disconnected count matrix:\n{self.count_matrix} '
                                      f'with reversible Bayesian HMM sampler using lag={self.lag}'
                                      f' and stride={self.stride}. Consider using shorter lag, '
                                      'or shorter stride (to use more of the data), '
                                      'or using a lower value for mincount_connectivity.')

        # here we blow up the output matrix (if needed) to the FULL state space because we want to use dtrajs in the
        # Bayesian HMM sampler. This is just an initialization.
        n_states_full = number_of_states(dtrajs)

        if prior.n_observation_states < n_states_full:
            eps = 0.01 / n_states_full  # default output probability, in order to avoid zero columns
            # full state space output matrix. make sure there are no zero columns
            B_init = eps * np.ones((self.n_states, n_states_full), dtype=np.float64)
            # fill active states
            B_init[:, prior.observation_state_symbols] = np.maximum(eps, prior.observation_probabilities)
            # renormalize B to make it row-stochastic
            B_init /= B_init.sum(axis=1)[:, None]
        else:
            B_init = prior.observation_probabilities

        # HMM sampler
        if self.init_hmsm is not None:
            hmm_mle = self.init_hmsm.bhmm_model
        else:
            hmm_mle = discrete_hmm(prior.initial_distribution, prior.transition_matrix, B_init)

        sampled_hmm = bayesian_hmm(dtrajs_lagged_strided, hmm_mle, nsample=self.n_samples,
                                   reversible=self.reversible, stationary=self.stationary,
                                   p0_prior=self.p0_prior, transition_matrix_prior=self.transition_matrix_prior,
                                   store_hidden=self.store_hidden, callback=callback).fetch_model()

        # repackage samples as HMSM objects and re-normalize after restricting to observable set
        samples = []
        for sample in sampled_hmm:  # restrict to observable set if necessary
            P = sample.transition_matrix
            pi = sample.stationary_distribution
            pobs = sample.output_model.output_probabilities
            init_dist = sample.initial_distribution

            Bobs = pobs[:, prior.observation_state_symbols]
            pobs = Bobs / Bobs.sum(axis=1)[:, None]  # renormalize
            samples.append(HiddenMarkovStateModel(P, pobs, stationary_distribution=pi,
                                                  count_model=prior_count_model, initial_counts=sample.initial_count,
                                                  reversible=self.reversible, initial_distribution=init_dist))

        # store results
        if self.store_hidden:
            model.hidden_state_trajectories_samples = [s.hidden_state_trajectories for s in sampled_hmm]
        model.samples = samples

        # set new model
        self._model = model

        return self
