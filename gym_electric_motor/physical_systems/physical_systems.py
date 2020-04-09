import numpy as np
from gym.spaces import Box

from ..core import PhysicalSystem
from ..physical_systems import electric_motors as em, mechanical_loads as ml, converters as cv, \
    voltage_supplies as vs, noise_generators as ng, solvers as sv
from ..utils import instantiate, set_state_array


class SCMLSystem(PhysicalSystem):
    """
    The SCML(Supply-Converter-Motor-Load)-System is used for the simulation of a technical setting consisting of these
    components as well as a noise generator and a solver for the electrical ODE of the motor and mechanical ODE of the
    load.
    """
    OMEGA_IDX = 0
    TORQUE_IDX = 1
    CURRENTS_IDX = []
    VOLTAGES_IDX = []
    U_SUP_IDX = -1

    @property
    def limits(self):
        return self._limits

    @property
    def nominal_state(self):
        return self._nominal_state

    @property
    def supply(self):
        """
        The voltage supply instance in the physical system
        """
        return self._supply

    @property
    def converter(self):
        """
        The power electronic converter instance in the system
        """
        return self._converter

    @property
    def electrical_motor(self):
        """
        The electrical motor instance of the system
        """
        return self._electrical_motor

    @property
    def mechanical_load(self):
        """
        The mechanical load instance in the system
        """
        return self._mechanical_load

    def __init__(
        self, converter, motor, load=None, supply='IdealVoltageSupply', ode_solver='euler', solver_kwargs=None,
        noise_generator=None, tau=1e-4, **kwargs
    ):
        """
        Args:
            converter(PowerElectronicConverter): Converter for the physical system
            motor(ElectricMotor): Motor of the system
            load(MechanicalLoad): Mechanical Load of the System
            supply(VoltageSupply): Voltage Supply
            ode_solver(OdeSolver): Ode Solver to use in this setting
            solver_kwargs(dict): Special keyword arguments to be passed to the solver
            noise_generator(NoiseGenerator):  Noise generator
            tau(float): discrete time step of the system
            kwargs(dict): Further arguments to pass to the modules while instantiation
        """
        self._converter = instantiate(cv.PowerElectronicConverter, converter, tau=tau, **kwargs)
        self._electrical_motor = instantiate(em.ElectricMotor, motor, tau=tau, **kwargs)
        load = load or ml.PolynomialStaticLoad(tau=tau, **kwargs)
        self._mechanical_load = instantiate(ml.MechanicalLoad, load, tau=tau, **kwargs)
        # If no special u_sup was passed, select the voltage limit of the motor
        if 'u_sup' in kwargs.keys():
            u_sup = kwargs['u_sup']
        else:
            u_sup = self._electrical_motor.limits['u']
        self._supply = instantiate(vs.VoltageSupply, supply, u_nominal=u_sup, tau=tau, **kwargs)
        noise_generator = noise_generator or ng.GaussianWhiteNoiseGenerator(tau=tau, **kwargs)
        self._noise_generator = instantiate(ng.NoiseGenerator, noise_generator, **kwargs)
        state_names = self._build_state_names()
        self._noise_generator.set_state_names(state_names)
        solver_kwargs = solver_kwargs or {}
        self._ode_solver = instantiate(sv.OdeSolver, ode_solver, **solver_kwargs)
        self._ode_solver.set_system_equation(self._system_equation)
        self._mechanical_load.set_j_rotor(self._electrical_motor.motor_parameter['j_rotor'])
        self._t = 0
        self._set_indices()
        state_space = self._build_state_space(state_names)
        super().__init__(self._converter.action_space, state_space, state_names, tau)
        self._limits = np.zeros_like(state_names, dtype=float)
        self._nominal_state = np.zeros_like(state_names, dtype=float)
        self._set_limits()
        self._set_nominal_state()
        self._noise_generator.set_signal_power_level(self._nominal_state)

    def _set_limits(self):
        """
        Method to set the physical limits from the modules.
        """
        for ind, state in enumerate(self._state_names):
            motor_lim = self._electrical_motor.limits.get(state, np.inf)
            mechanical_lim = self._mechanical_load.limits.get(state, np.inf)
            self._limits[ind] = min(motor_lim, mechanical_lim)
        self._limits[self._state_positions['u_sup']] = self.supply.u_nominal

    def _set_nominal_state(self):
        """
        Method to set the nominal values from the modules.
        """
        for ind, state in enumerate(self._state_names):
            motor_nom = self._electrical_motor.nominal_values.get(state, np.inf)
            mechanical_nom = self._mechanical_load.nominal_values.get(state, np.inf)
            self._nominal_state[ind] = min(motor_nom, mechanical_nom)
        self._nominal_state[self._state_positions['u_sup']] = self.supply.u_nominal

    def _build_state_space(self, state_names):
        """
        Method to build the normalized state space (i.e. the maximum and minimum possible values for each state variable
        normalized by the limits).

        Args:
            state_names(list(str)): list of the names of each state.
        """
        raise NotImplementedError

    def _build_state_names(self):
        """
        Setting of the state names in the physical system.
        """
        raise NotImplementedError

    def _set_indices(self):
        """
        Setting of indices to faster access the arrays during integration.
        """
        self._omega_ode_idx = self._mechanical_load.OMEGA_IDX
        self._load_ode_idx = list(range(len(self._mechanical_load.state_names)))
        self._ode_currents_idx = list(range(
            self._load_ode_idx[-1] + 1, self._load_ode_idx[-1] + 1 + len(self._electrical_motor.CURRENTS)
        ))
        self._motor_ode_idx = self._ode_currents_idx
        self.OMEGA_IDX = self.mechanical_load.OMEGA_IDX
        self.TORQUE_IDX = len(self.mechanical_load.state_names)
        currents_lower = self.TORQUE_IDX + 1
        currents_upper = currents_lower + len(self._electrical_motor.CURRENTS)
        self.CURRENTS_IDX = list(range(currents_lower, currents_upper))
        voltages_lower = currents_upper
        voltages_upper = voltages_lower + len(self._electrical_motor.VOLTAGES)
        self.VOLTAGES_IDX = list(range(voltages_lower, voltages_upper))
        self.U_SUP_IDX = voltages_upper

    def simulate(self, action, *_, **__):
        # Docstring of superclass
        ode_state = self._ode_solver.y
        i_in = self._electrical_motor.i_in(ode_state[self._ode_currents_idx])
        switching_times = self._converter.set_action(action, self._t)
        i_sup = self._converter.i_sup(i_in)
        u_sup = self._supply.get_voltage(self._t, i_sup)

        for t in switching_times[:-1]:
            u_in = self._converter.convert(i_in, self._ode_solver.t)
            u_in = [u * u_sup for u in u_in]
            self._ode_solver.set_f_params(u_in)
            ode_state = self._ode_solver.integrate(t)
            i_in = self._electrical_motor.i_in(ode_state[self._ode_currents_idx])

        u_in = self._converter.convert(i_in, self._ode_solver.t)
        u_in = [u * u_sup for u in u_in]
        self._ode_solver.set_f_params(u_in)
        ode_state = self._ode_solver.integrate(self._t + self._tau)
        self._t = self._ode_solver.t
        self._k += 1
        torque = self._electrical_motor.torque(ode_state[self._motor_ode_idx])
        noise = self._noise_generator.noise()
        system_state = np.zeros_like(self.state_names, dtype=float)
        motor_state = ode_state[len(self.mechanical_load.state_names):]
        system_state[:len(self._mechanical_load.state_names)] = ode_state[:len(self._mechanical_load.state_names)]
        system_state[self.TORQUE_IDX] = torque
        system_state[self.CURRENTS_IDX] = motor_state[self._electrical_motor.CURRENTS_IDX]
        system_state[self.VOLTAGES_IDX] = u_in
        system_state[self.U_SUP_IDX] = u_sup
        return (system_state + noise) / self._limits

    def _system_equation(self, t, state, u_in, **__):
        """
        Systems differential equation system.

        It is a concatenation of the motors electrical ode system and the mechanical ode system.

        Args:
            t(float): Current systems time
            state(ndarray(float)): Current systems ODE-State
            u_in(list(float)): Input voltages from the converter

        Returns:
            ndarray(float): The derivatives of the ODE-State. Based on this, the Ode Solver calculates the next state.
        """
        motor_derivative = self._electrical_motor.electrical_ode(
            state[self._motor_ode_idx], u_in, state[self._omega_ode_idx]
        )
        torque = self._electrical_motor.torque(state[self._motor_ode_idx])
        load_derivative = self._mechanical_load.mechanical_ode(t, state[self._load_ode_idx], torque)
        return np.concatenate((load_derivative, motor_derivative))

    def reset(self, *_):
        """
        Reset all the systems modules to an initial state.

        Returns:
             The new state of the system.
        """
        motor_state = self._electrical_motor.reset()
        mechanical_state = self._mechanical_load.reset()
        ode_state = np.concatenate((mechanical_state, motor_state))
        u_sup = self.supply.reset()
        u_in = self.converter.reset()
        u_in = [u * u_sup for u in u_in]
        torque = self.electrical_motor.torque(motor_state)
        noise = self._noise_generator.reset()
        self._t = 0
        self._k = 0
        self._ode_solver.set_initial_value(ode_state, self._t)
        system_state = np.concatenate((
            ode_state[:len(self._mechanical_load.state_names)],
            [torque],
            motor_state[self._electrical_motor.CURRENTS_IDX],
            u_in,
            [u_sup]
        ))
        return (system_state + noise) / self._limits


class DcMotorSystem(SCMLSystem):
    """
    SCML-System that can be used for all DC Motors.
    """

    def _build_state_names(self):
        # Docstring of superclass
        return (
            self._mechanical_load.state_names
            + ['torque']
            + self._electrical_motor.CURRENTS
            + self._electrical_motor.VOLTAGES
            + ['u_sup']
        )

    def _build_state_space(self, state_names):
        # Docstring of superclass
        low, high = self._electrical_motor.get_state_space(self._converter.currents, self._converter.voltages)
        low_mechanical, high_mechanical = self._mechanical_load.get_state_space((low['omega'], high['omega']))
        low.update(low_mechanical)
        high.update(high_mechanical)
        high['u_sup'] = self._supply.supply_range[1] / self._supply.u_nominal
        if self._supply.supply_range[0] != self._supply.supply_range[1]:
            low['u_sup'] = self._supply.supply_range[0] / self._supply.u_nominal
        else:
            low['u_sup'] = 0
        low = set_state_array(low, state_names)
        high = set_state_array(high, state_names)
        return Box(low, high)


class SynchronousMotorSystem(SCMLSystem):
    """
    SCML-System that can be used with all Synchronous Motors
    """

    def __init__(self, control_space='abc', **kwargs):
        """
        Args:
            control_space(str):('abc' or 'dq') Choose, if actions the actions space is in dq or abc space
            kwargs: Further arguments to pass tp SCMLSystem
        """
        super().__init__(**kwargs)
        self.control_space = control_space
        if control_space == 'dq':
            self._action_space = Box(-1, 1, shape=(2,))

    def _build_state_space(self, state_names):
        # Docstring of superclass
        low = -1 * np.ones_like(state_names, dtype=float)
        low[self.U_SUP_IDX] = 0.0
        high = np.ones_like(state_names, dtype=float)
        return Box(low, high)

    def _build_state_names(self):
        # Docstring of superclass
        return (
            self._mechanical_load.state_names
            + ['torque']
            + ['i_a'] + ['i_b'] + ['i_c'] + ['i_sq'] + ['i_sd']
            + ['u_a'] + ['u_b'] + ['u_c'] + ['u_sq'] + ['u_sd']
            + ['epsilon']
            + ['u_sup']
        )

    def _set_indices(self):
        # Docstring of superclass
        self._omega_ode_idx = self._mechanical_load.OMEGA_IDX
        self._load_ode_idx = list(range(len(self._mechanical_load.state_names)))
        self._ode_currents_idx = list(range(
            self._load_ode_idx[-1] + 1, self._load_ode_idx[-1] + 1 + len(self._electrical_motor.CURRENTS)
        ))
        self._motor_ode_idx = self._ode_currents_idx
        self._motor_ode_idx += [self._motor_ode_idx[-1] + 1]
        self._ode_currents_idx = self._motor_ode_idx[:-1]
        self.OMEGA_IDX = self.mechanical_load.OMEGA_IDX
        self.TORQUE_IDX = len(self.mechanical_load.state_names)
        currents_lower = self.TORQUE_IDX + 1
        currents_upper = currents_lower + 5
        self.CURRENTS_IDX = list(range(currents_lower, currents_upper))
        voltages_lower = currents_upper
        voltages_upper = voltages_lower + 5
        self.VOLTAGES_IDX = list(range(voltages_lower, voltages_upper))
        self.EPSILON_IDX = voltages_upper
        self.U_SUP_IDX = self.EPSILON_IDX + 1
        self._ode_epsilon_idx = self._motor_ode_idx[-1]

    def abc_to_dq_space(self, abc_quantities, epsilon_el, normed_epsilon=False):
        """
        Transformation from abc to dq space

        Args:
            abc_quantities: Three quantities in abc-space (e.g. (u_a, u_b, u_c) or (i_a, i_b, i_c))
            epsilon_el: Electrical angle of the motor
            normed_epsilon(bool): True, if epsilon is normed to [-1,1] else in [-pi, pi] (default)

        Returns:
            (quantity_q, quantity_d): The quantities in the dq-space
        """
        if normed_epsilon:
            epsilon_el *= np.pi
        dq_quantity = self._electrical_motor.q_inv(self._electrical_motor.t_23(abc_quantities), epsilon_el)
        return dq_quantity[::-1]

    def dq_to_abc_space(self, dq_quantities, epsilon_el, normed_epsilon=False):
        """
        Transformation from dq to abc space

        Args:
            dq_quantities: Three quantities in dq-space (e.g. (u_q, u_d) or (i_q, i_d))
            epsilon_el: Electrical angle of the motor
            normed_epsilon(bool): True, if epsilon is normed to [-1,1] else in [-pi, pi] (default)

        Returns:
            (quantity_a, quantity_b, quantity_c): The quantities in the abc-space
        """
        if normed_epsilon:
            epsilon_el *= np.pi
        return self._electrical_motor.t_32(self._electrical_motor.q(dq_quantities[::-1], epsilon_el))

    def simulate(self, action, *_, **__):
        # Docstring of superclass
        ode_state = self._ode_solver.y
        eps = ode_state[self._ode_epsilon_idx]
        if self.control_space == 'dq':
            action = self.dq_to_abc_space(action, eps)
        i_in = self.dq_to_abc_space(self._electrical_motor.i_in(ode_state[self._ode_currents_idx]), eps)
        switching_times = self._converter.set_action(action, self._t)
        i_sup = self._converter.i_sup(i_in)
        u_sup = self._supply.get_voltage(self._t, i_sup)

        for t in switching_times[:-1]:
            u_in = self._converter.convert(i_in, self._ode_solver.t)
            u_in = [u * u_sup for u in u_in]
            u_qd = self.abc_to_dq_space(u_in, eps)
            self._ode_solver.set_f_params(u_qd)
            ode_state = self._ode_solver.integrate(t)
            eps = ode_state[self._ode_epsilon_idx]
            i_in = self.dq_to_abc_space(self._electrical_motor.i_in(ode_state[self._ode_currents_idx]), eps)
        u_in = self._converter.convert(i_in, self._ode_solver.t)
        u_in = [u * u_sup for u in u_in]
        u_qd = self.abc_to_dq_space(u_in, eps)
        self._ode_solver.set_f_params(u_qd)
        ode_state = self._ode_solver.integrate(self._t + self._tau)
        self._t = self._ode_solver.t
        self._k += 1
        torque = self._electrical_motor.torque(ode_state[self._motor_ode_idx])
        noise = self._noise_generator.noise()
        mechanical_state = ode_state[self._load_ode_idx]
        i_qd = ode_state[self._ode_currents_idx]
        i_abc = list(
            self.dq_to_abc_space(i_qd, eps)
        )
        eps = ode_state[self._ode_epsilon_idx] % (2 * np.pi)
        if eps > np.pi:
            eps -= 2 * np.pi

        system_state = np.concatenate((
            mechanical_state,
            [torque],
            i_abc, i_qd,
            u_in, u_qd,
            [eps],
            [u_sup]
        ))
        return (system_state + noise) / self._limits

    def reset(self, *_):
        # Docstring of superclass
        motor_state = self._electrical_motor.reset()
        mechanical_state = self._mechanical_load.reset()
        ode_state = np.concatenate((mechanical_state, motor_state))
        u_sup = self.supply.reset()
        eps = ode_state[self._ode_epsilon_idx]
        if eps > np.pi:
            eps -= 2 * np.pi
        u_abc = self.converter.reset()
        u_abc = [u * u_sup for u in u_abc]
        u_qd = self.abc_to_dq_space(u_abc, eps)
        i_qd = ode_state[self._ode_currents_idx]
        i_abc = self.dq_to_abc_space(i_qd, eps)
        torque = self.electrical_motor.torque(motor_state)
        noise = self._noise_generator.reset()
        self._t = 0
        self._k = 0
        self._ode_solver.set_initial_value(ode_state, self._t)
        system_state = np.array(
            list(mechanical_state)
            + [torque]
            + list(i_abc) + list(i_qd)
            + list(u_abc) + list(u_qd)
            + [eps]
            + [u_sup]
        )
        return (system_state + noise) / self._limits

class SquirrelCageInductionMotorSystem(SCMLSystem):
    """
    SCML-System that can be used with all Synchronous Motors
    """
    def __init__(self, control_space='abc', ode_solver='scipy.ode', **kwargs):
        """
        Args:
            control_space(str):('abc' or 'dq') Choose, if actions the actions space is in dq or abc space
            kwargs: Further arguments to pass tp SCMLSystem
        """
        super().__init__(ode_solver=ode_solver, **kwargs)
        self.control_space = control_space
        if control_space == 'dq':
            self._action_space = Box(-1, 1, shape=(2,))

    def _build_state_space(self, state_names):
        # Docstring of superclass
        low = -1 * np.ones_like(state_names, dtype=float)
        low[self.U_SUP_IDX] = 0.0
        high = np.ones_like(state_names, dtype=float)
        return Box(low, high)

    def _build_state_names(self):
        # Docstring of superclass
        return (
            self._mechanical_load.state_names
            + ['torque']
            + ['i_sa'] + ['i_sb'] + ['i_sc'] + ['i_sq'] + ['i_sd']
            + ['u_sa'] + ['u_sb'] + ['u_sc'] + ['u_sq'] + ['u_sd']
            + ['epsilon']
            + ['u_sup']
        )

    def _set_indices(self):
        # Docstring of superclass
        super()._set_indices()
        self._motor_ode_idx += [self._motor_ode_idx[-1] + 1] + [self._motor_ode_idx[-1] + 2]
        self._motor_ode_idx += [self._motor_ode_idx[-1] + 1]

        self._ode_currents_idx = self._motor_ode_idx[:-3]
        self._ode_flux_idx = self._motor_ode_idx[-3:-1]

        self.OMEGA_IDX = self.mechanical_load.OMEGA_IDX
        self.TORQUE_IDX = len(self.mechanical_load.state_names)
        currents_lower = self.TORQUE_IDX + 1
        currents_upper = currents_lower + 5
        self.CURRENTS_IDX = list(range(currents_lower, currents_upper))
        voltages_lower = currents_upper
        voltages_upper = voltages_lower + 5
        self.VOLTAGES_IDX = list(range(voltages_lower, voltages_upper))
        self.EPSILON_IDX = voltages_upper
        self.U_SUP_IDX = self.EPSILON_IDX + 1
        self._ode_epsilon_idx = self._motor_ode_idx[-1]

    def abc_to_alphabeta_space(self, abc_quantities):
        """
        Transformation from abc to alphabeta space

        Args:
            abc_quantities: Three quantities in abc-space (e.g. (u_a, u_b, u_c) or (i_a, i_b, i_c))

        Returns:
            (quantity_alpha, quantity_beta): The quantities in the alphabeta-space
        """
        alphabeta_quantity = self._electrical_motor.t_23(abc_quantities)
        return alphabeta_quantity

    def alphabeta_to_abc_space(self, alphabeta_quantities):
        """
        Transformation from dq to abc space

        Args:
            alphabeta_quantities: Two quantities in alphabeta-space (e.g. (u_alpha, u_beta) or (i_alpha, i_beta))

        Returns:
            (quantity_a, quantity_b, quantity_c): The quantities in the abc-space
        """
        return self._electrical_motor.t_32(alphabeta_quantities)

    def abc_to_dq_space(self, abc_quantities, epsilon_el, normed_epsilon=False):
        """
        Transformation from abc to dq space

        Args:
            abc_quantities: Three quantities in abc-space (e.g. (u_a, u_b, u_c) or (i_a, i_b, i_c))
            epsilon_el: Electrical angle of the motor
            normed_epsilon(bool): True, if epsilon is normed to [-1,1] else in [-pi, pi] (default)

        Returns:
            (quantity_q, quantity_d): The quantities in the dq-space
        """
        if normed_epsilon:
            epsilon_el *= np.pi
        dq_quantity = self._electrical_motor.q_inv(self._electrical_motor.t_23(abc_quantities), epsilon_el)
        return dq_quantity[::-1]

    def dq_to_abc_space(self, dq_quantities, epsilon_el, normed_epsilon=False):
        """
        Transformation from dq to abc space

        Args:
            dq_quantities: Three quantities in dq-space (e.g. (u_q, u_d) or (i_q, i_d))
            epsilon_el: Electrical angle of the motor
            normed_epsilon(bool): True, if epsilon is normed to [-1,1] else in [-pi, pi] (default)

        Returns:
            (quantity_a, quantity_b, quantity_c): The quantities in the abc-space
        """
        if normed_epsilon:
            epsilon_el *= np.pi
        return self._electrical_motor.t_32(self._electrical_motor.q(dq_quantities[::-1], epsilon_el))

    def alphabeta_to_dq_space(self, alphabeta_quantities, epsilon_el, normed_epsilon=False):
        """
        Transformation from alphabeta to dq space

        Args:
            alphabeta_quantities: Two quantities in alphabeta-space (e.g. (u_alpha, u_beta) or (i_alpha, i_beta))
            epsilon_el: Electrical angle of the motor
            normed_epsilon(bool): True, if epsilon is normed to [-1,1] else in [-pi, pi] (default)

        Returns:
            (quantity_q, quantity_d): The quantities in the dq-space
        """
        if normed_epsilon:
            epsilon_el *= np.pi
        dq_quantity = self._electrical_motor.q_inv(alphabeta_quantities, epsilon_el)
        return dq_quantity[::-1]

    def dq_to_alphabeta_space(self, dq_quantities, epsilon_el, normed_epsilon=False):
        """
        Transformation from dq to alphabeta space

        Args:
            dq_quantities: Two quantities in dq-space (e.g. (u_q, u_d) or (i_q, i_d))
            epsilon_el: Electrical angle of the motor
            normed_epsilon(bool): True, if epsilon is normed to [-1,1] else in [-pi, pi] (default)

        Returns:
            (quantity_alpha, quantity_beta): The quantities in the alphabeta-space
        """
        if normed_epsilon:
            epsilon_el *= np.pi
        return self._electrical_motor.q(dq_quantities[::-1], epsilon_el)

    def calculate_field_angle(self, state):
        psi_ralpha = state[self._ode_flux_idx[0]]
        psi_rbeta = state[self._ode_flux_idx[1]]
        eps_fs = np.arctan2(psi_rbeta, psi_ralpha)
        return eps_fs

    def simulate(self, action, *_, **__):
        # Docstring of superclass
        ode_state = self._ode_solver.y

        eps_fs = self.calculate_field_angle(ode_state)

        if self.control_space == 'dq':
            action = self.dq_to_abc_space(action, eps_fs)

        i_in = self.alphabeta_to_abc_space(self._electrical_motor.i_in(ode_state[self._ode_currents_idx]))
        switching_times = self._converter.set_action(action, self._t)
        i_sup = self._converter.i_sup(i_in)
        u_sup = self._supply.get_voltage(self._t, i_sup)

        for t in switching_times[:-1]:
            u_in = self._converter.convert(i_in, self._ode_solver.t)
            u_in = [u * u_sup for u in u_in]
            u_qd = self.abc_to_dq_space(u_in, eps_fs)
            u_alphabeta = self.abc_to_alphabeta_space(u_in)
            self._ode_solver.set_f_params(u_alphabeta)
            ode_state = self._ode_solver.integrate(t)
            eps_fs = self.calculate_field_angle(ode_state)
            i_in = self.alphabeta_to_abc_space(self._electrical_motor.i_in(ode_state[self._ode_currents_idx]))

        u_in = self._converter.convert(i_in, self._ode_solver.t)
        u_in = [u * u_sup for u in u_in]
        u_qd = self.abc_to_dq_space(u_in, eps_fs)
        u_alphabeta = self.abc_to_alphabeta_space(u_in)
        self._ode_solver.set_f_params(u_alphabeta)
        ode_state = self._ode_solver.integrate(self._t + self._tau)
        self._t = self._ode_solver.t
        self._k += 1
        torque = self._electrical_motor.torque(ode_state[self._motor_ode_idx])
        noise = self._noise_generator.noise()
        mechanical_state = ode_state[self._load_ode_idx]
        i_qd = self.alphabeta_to_dq_space(ode_state[self._ode_currents_idx], eps_fs)
        i_abc = list(self.dq_to_abc_space(i_qd, eps_fs))

        eps = ode_state[self._ode_epsilon_idx] % (2 * np.pi)
        if eps > np.pi:
            eps -= 2 * np.pi

        system_state = np.concatenate((
            mechanical_state,
            [torque],
            i_abc, i_qd,
            u_in, u_qd,
            [eps],
            [u_sup]
        ))
        return (system_state + noise) / self._limits

    def reset(self, *_):
        # Docstring of superclass
        motor_state = self._electrical_motor.reset()
        mechanical_state = self._mechanical_load.reset()
        ode_state = np.concatenate((mechanical_state, motor_state))
        u_sup = self.supply.reset()

        eps = ode_state[self._ode_epsilon_idx]
        eps_fs = self.calculate_field_angle(ode_state)

        if eps > np.pi:
            eps -= 2 * np.pi

        u_abc = self.converter.reset()
        u_abc = [u * u_sup for u in u_abc]
        u_qd = self.abc_to_dq_space(u_abc, eps_fs)
        i_qd = self.alphabeta_to_dq_space(ode_state[self._ode_currents_idx], eps_fs)
        i_abc = self.dq_to_abc_space(i_qd, eps_fs)
        torque = self.electrical_motor.torque(motor_state)
        noise = self._noise_generator.reset()
        self._t = 0
        self._k = 0
        self._ode_solver.set_initial_value(ode_state, self._t)
        system_state = np.array(
            list(mechanical_state)
            + [torque]
            + list(i_abc) + list(i_qd)
            + list(u_abc) + list(u_qd)
            + [eps]
            + [u_sup]
        )
        return (system_state + noise) / self._limits

class DoublyFedInductionMotorSystem(SCMLSystem):
    """
    SCML-System that can be used with the Doubly Fed Induction Motor
    """
    def __init__(self, control_space='abc', ode_solver='scipy.ode', **kwargs):
        """
        Args:
            control_space(str):('abc' or 'dq') Choose, if actions the actions space is in dq or abc space
            kwargs: Further arguments to pass tp SCMLSystem
        """
        super().__init__(ode_solver=ode_solver, **kwargs)
        self.control_space = control_space
        if control_space == 'dq':
            self._action_space = Box(-1, 1, shape=(4,))

    def _set_limits(self):
        """
        Method to set the physical limits from the modules.
        """
        for ind, state in enumerate(self._state_names):
            motor_lim = self._electrical_motor.limits.get(state, np.inf)
            mechanical_lim = self._mechanical_load.limits.get(state, np.inf)
            self._limits[ind] = min(motor_lim, mechanical_lim)
        self._limits[self._state_positions['u_sup']] = self.supply.u_nominal

    def _build_state_space(self, state_names):
        # Docstring of superclass
        low = -1 * np.ones_like(state_names, dtype=float)
        low[self.U_SUP_IDX] = 0.0
        high = np.ones_like(state_names, dtype=float)
        return Box(low, high)

    def _build_state_names(self):
        # Docstring of superclass
        return (
            self._mechanical_load.state_names
            + ['torque']
            + ['i_sa'] + ['i_sb'] + ['i_sc'] + ['i_sq'] + ['i_sd']
            + ['i_ra'] + ['i_rb'] + ['i_rc'] + ['i_rq'] + ['i_rd']
            + ['u_sa'] + ['u_sb'] + ['u_sc'] + ['u_sq'] + ['u_sd']
            + ['u_ra'] + ['u_rb'] + ['u_rc'] + ['u_rq'] + ['u_rd']
            + ['epsilon']
            + ['u_sup']
        )

    def _set_indices(self):
        # Docstring of superclass
        super()._set_indices()
        self._motor_ode_idx += [self._motor_ode_idx[-1] + 1] + [self._motor_ode_idx[-1] + 2]
        self._motor_ode_idx += [self._motor_ode_idx[-1] + 1]

        self._ode_currents_idx = self._motor_ode_idx[:-3]
        self._ode_flux_idx = self._motor_ode_idx[-3:-1]

        self.OMEGA_IDX = self.mechanical_load.OMEGA_IDX
        self.TORQUE_IDX = len(self.mechanical_load.state_names)
        currents_lower = self.TORQUE_IDX + 1
        currents_upper = currents_lower + 10
        self.CURRENTS_IDX = list(range(currents_lower, currents_upper))
        voltages_lower = currents_upper
        voltages_upper = voltages_lower + 10
        self.VOLTAGES_IDX = list(range(voltages_lower, voltages_upper))
        self.EPSILON_IDX = voltages_upper
        self.U_SUP_IDX = self.EPSILON_IDX + 1
        self._ode_epsilon_idx = self._motor_ode_idx[-1]

    def abc_to_alphabeta_space(self, abc_quantities):
        """
        Transformation from abc to alphabeta space

        Args:
            abc_quantities: Three quantities in abc-space (e.g. (u_a, u_b, u_c) or (i_a, i_b, i_c))

        Returns:
            (quantity_alpha, quantity_beta): The quantities in the alphabeta-space
        """
        alphabeta_quantity = self._electrical_motor.t_23(abc_quantities)
        return alphabeta_quantity

    def alphabeta_to_abc_space(self, alphabeta_quantities):
        """
        Transformation from dq to abc space

        Args:
            alphabeta_quantities: Two quantities in alphabeta-space (e.g. (u_alpha, u_beta) or (i_alpha, i_beta))

        Returns:
            (quantity_a, quantity_b, quantity_c): The quantities in the abc-space
        """
        return self._electrical_motor.t_32(alphabeta_quantities)

    def abc_to_dq_space(self, abc_quantities, epsilon_el, normed_epsilon=False):
        """
        Transformation from abc to dq space

        Args:
            abc_quantities: Three quantities in abc-space (e.g. (u_a, u_b, u_c) or (i_a, i_b, i_c))
            epsilon_el: Electrical angle of the motor
            normed_epsilon(bool): True, if epsilon is normed to [-1,1] else in [-pi, pi] (default)

        Returns:
            (quantity_q, quantity_d): The quantities in the dq-space
        """
        if normed_epsilon:
            epsilon_el *= np.pi
        dq_quantity = self._electrical_motor.q_inv(self._electrical_motor.t_23(abc_quantities), epsilon_el)
        return dq_quantity[::-1]

    def dq_to_abc_space(self, dq_quantities, epsilon_el, normed_epsilon=False):
        """
        Transformation from dq to abc space

        Args:
            dq_quantities: Three quantities in dq-space (e.g. (u_q, u_d) or (i_q, i_d))
            epsilon_el: Electrical angle of the motor
            normed_epsilon(bool): True, if epsilon is normed to [-1,1] else in [-pi, pi] (default)

        Returns:
            (quantity_a, quantity_b, quantity_c): The quantities in the abc-space
        """
        if normed_epsilon:
            epsilon_el *= np.pi
        return self._electrical_motor.t_32(self._electrical_motor.q(dq_quantities[::-1], epsilon_el))

    def alphabeta_to_dq_space(self, alphabeta_quantities, epsilon_el, normed_epsilon=False):
        """
        Transformation from alphabeta to dq space

        Args:
            alphabeta_quantities: Two quantities in alphabeta-space (e.g. (u_alpha, u_beta) or (i_alpha, i_beta))
            epsilon_el: Electrical angle of the motor
            normed_epsilon(bool): True, if epsilon is normed to [-1,1] else in [-pi, pi] (default)

        Returns:
            (quantity_q, quantity_d): The quantities in the dq-space
        """
        if normed_epsilon:
            epsilon_el *= np.pi
        dq_quantity = self._electrical_motor.q_inv(alphabeta_quantities, epsilon_el)
        return dq_quantity[::-1]

    def dq_to_alphabeta_space(self, dq_quantities, epsilon_el, normed_epsilon=False):
        """
        Transformation from dq to alphabeta space

        Args:
            dq_quantities: Two quantities in dq-space (e.g. (u_q, u_d) or (i_q, i_d))
            epsilon_el: Electrical angle of the motor
            normed_epsilon(bool): True, if epsilon is normed to [-1,1] else in [-pi, pi] (default)

        Returns:
            (quantity_alpha, quantity_beta): The quantities in the alphabeta-space
        """
        if normed_epsilon:
            epsilon_el *= np.pi
        return self._electrical_motor.q(dq_quantities[::-1], epsilon_el)

    def calculate_field_angle(self, state):
        psi_ralpha = state[self._ode_flux_idx[0]]
        psi_rbeta = state[self._ode_flux_idx[1]]
        eps_fs = np.arctan2(psi_rbeta, psi_ralpha)
        return eps_fs

    def calculate_rotor_current(self, state):
        mp = self._electrical_motor.motor_parameter
        l_r = mp['l_m'] + mp['l_rsig']

        i_salpha = state[self._ode_currents_idx[0]]
        i_sbeta = state[self._ode_currents_idx[1]]
        psi_ralpha = state[self._ode_flux_idx[0]]
        psi_rbeta = state[self._ode_flux_idx[1]]

        i_ralpha = 1 / l_r * psi_ralpha - mp['l_m'] / l_r * i_salpha
        i_rbeta = 1 / l_r * psi_rbeta - mp['l_m'] / l_r * i_sbeta
        return [i_ralpha, i_rbeta]


    def simulate(self, action, *_, **__):
        # Docstring of superclass

        # Coordinate Systems used here:
        # alphabeta refers to the stator-fixed two-phase reference frame
        # gammadelta refers to the rotor-fixed two-phase reference frame
        # abc refers to the stator-fixed three-phase reference frame
        # def refers to the rotor-fixed three-phase reference frame
        # dq refers to the field-oriented (two-phase) reference frame
        # e.g. u_rdef is the rotor voltage representation in the rotor-fixed three-phase reference frame
        # u_rabc ist the rotor voltage representation in the stator-fixed three-phase reference frame

        ode_state = self._ode_solver.y

        eps_field = self.calculate_field_angle(ode_state)
        eps_el = ode_state[self._ode_epsilon_idx]

        if self.control_space == 'dq':
            action_stator = action[0:2]
            action_rotor = action[2:4]
            action_stator = self.dq_to_abc_space(action_stator, eps_field)
            action_rotor = self.dq_to_abc_space(action_rotor, eps_field)
            action = np.concatenate((action_stator, action_rotor)).tolist()

        i_sabc = self.alphabeta_to_abc_space(self._electrical_motor.i_in(ode_state[self._ode_currents_idx]))
        i_rdef = self.alphabeta_to_abc_space(self.calculate_rotor_current(ode_state))
        switching_times = self._converter.set_action(action, self._t)
        i_s_sup = self._converter.i_sup(i_sabc)
        i_r_sup = self._converter.i_sup(i_rdef)
        i_sup = i_s_sup + i_r_sup

        u_sup = self._supply.get_voltage(self._t, i_sup)

        for t in switching_times[:-1]:
            u_in = self._converter.convert([i_sabc, i_rdef], self._ode_solver.t)
            u_sabc = u_in[0]
            u_rdef = u_in[1]
            u_sabc = [u * u_sup for u in u_sabc]
            u_rdef = [u * u_sup for u in u_rdef]
            u_sqd = self.abc_to_dq_space(u_sabc, eps_field)
            u_rqd = self.abc_to_dq_space(u_rdef, eps_field-eps_el)
            u_salphabeta = self.abc_to_alphabeta_space(u_sabc)
            u_ralphabeta = self.dq_to_alphabeta_space(u_rqd, eps_field)

            u_sr_alphabeta = np.array([u_salphabeta, u_ralphabeta])
            self._ode_solver.set_f_params(u_sr_alphabeta)
            ode_state = self._ode_solver.integrate(t)

            eps_field = self.calculate_field_angle(ode_state)
            eps_el = ode_state[self._ode_epsilon_idx]
            i_sabc = self.alphabeta_to_abc_space(self._electrical_motor.i_in(ode_state[self._ode_currents_idx]))
            i_rdef = self.alphabeta_to_abc_space(self.calculate_rotor_current(ode_state))

        u_in = self._converter.convert(np.array([i_sabc, i_rdef]).tolist(), self._ode_solver.t)
        u_sabc = u_in[0]
        u_rdef = u_in[1]
        u_sabc = [u * u_sup for u in u_sabc]
        u_rdef = [u * u_sup for u in u_rdef]
        u_sqd = self.abc_to_dq_space(u_sabc, eps_field)
        u_rqd = self.abc_to_dq_space(u_rdef, eps_field-eps_el)
        u_salphabeta = self.abc_to_alphabeta_space(u_sabc)
        u_ralphabeta = self.dq_to_alphabeta_space(u_rqd, eps_field)

        u_sr_alphabeta = np.array([u_salphabeta, u_ralphabeta])
        self._ode_solver.set_f_params(u_sr_alphabeta)
        ode_state = self._ode_solver.integrate(self._t + self._tau)
        self._t = self._ode_solver.t
        self._k += 1
        torque = self._electrical_motor.torque(ode_state[self._motor_ode_idx])
        noise = self._noise_generator.noise()
        mechanical_state = ode_state[self._load_ode_idx]

        i_sqd = self.alphabeta_to_dq_space(ode_state[self._ode_currents_idx], eps_field)
        i_sabc = list(self.dq_to_abc_space(i_sqd, eps_field))

        i_rqd = self.alphabeta_to_dq_space(self.calculate_rotor_current(ode_state), eps_field)
        i_rdef = list(self.dq_to_abc_space(i_rqd, eps_field-eps_el))

        eps_el = ode_state[self._ode_epsilon_idx] % (2 * np.pi)
        if eps_el > np.pi:
            eps_el -= 2 * np.pi

        system_state = np.concatenate((
            mechanical_state,
            [torque],
            i_sabc, i_sqd,
            i_rdef, i_rqd,
            u_sabc, u_sqd,
            u_rdef, u_rqd,
            [eps_el],
            [u_sup],
        ))
        return (system_state + noise) / self._limits

    def reset(self, *_):
        # Docstring of superclass
        motor_state = self._electrical_motor.reset()
        mechanical_state = self._mechanical_load.reset()
        ode_state = np.concatenate((mechanical_state, motor_state))
        u_sup = self.supply.reset()

        eps_el = ode_state[self._ode_epsilon_idx]
        eps_field = self.calculate_field_angle(ode_state)

        if eps_el > np.pi:
            eps_el -= 2 * np.pi

        if eps_field > np.pi:
            eps_field -= 2 * np.pi

        u_sr_abc = self.converter.reset()
        u_sabc = u_sr_abc[0]
        u_rdef = u_sr_abc[1]
        u_sabc = [u * u_sup for u in u_sabc]
        u_rdef = [u * u_sup for u in u_rdef]
        u_sqd = self.abc_to_dq_space(u_sabc, eps_field)
        u_rqd = self.abc_to_dq_space(u_rdef, eps_field-eps_el)

        i_sqd = self.alphabeta_to_dq_space(ode_state[self._ode_currents_idx], eps_field)
        i_sabc = self.dq_to_abc_space(i_sqd, eps_field)


        i_rqd = self.alphabeta_to_dq_space(self.calculate_rotor_current(ode_state), eps_field-eps_el)
        i_rdef = self.dq_to_abc_space(i_rqd, eps_field-eps_el)

        torque = self.electrical_motor.torque(motor_state)
        noise = self._noise_generator.reset()
        self._t = 0
        self._k = 0
        self._ode_solver.set_initial_value(ode_state, self._t)
        system_state = np.array(
            list(mechanical_state)
            + [torque]
            + list(i_sabc) + list(i_sqd)
            + list(i_rdef) + list(i_rqd)
            + list(u_sabc) + list(u_sqd)
            + list(u_rdef) + list(u_rqd)
            + [eps_el]
            + [u_sup]
        )
        return (system_state + noise) / self._limits