#!/usr/bin/env python

import math

import yaml
import numpy as np
from scipy.spatial.transform import Rotation

from .cfsim import cffirmware as firm

# main class of simulation.
# crazyflies keep reference to this object to ask what time it is.
# also does the plotting.
#
class TimeHelper:
    def __init__(self, vis, dt, writecsv, disturbanceSize, maxVel=np.inf):
        if vis == "mpl":
            from .visualizer import visMatplotlib
            self.visualizer = visMatplotlib.VisMatplotlib()
        elif vis == "vispy":
            from .visualizer import visVispy
            self.visualizer = visVispy.VisVispy()
        elif vis == "null":
            from .visualizer import visNull
            self.visualizer = visNull.VisNull()
        else:
            raise Exception("Unknown visualization backend: {}".format(vis))
        self.t = 0.0
        self.dt = dt
        # Since our integration/animation ticks are always the fixed duration
        # dt, any call to sleep() with a non-multiple of dt will have some
        # "leftover" time. Keep track of it here and add extra ticks in future.
        self.sleepResidual = 0.0
        self.crazyflies = []
        self.disturbanceSize = disturbanceSize
        self.maxVel = maxVel
        if writecsv:
            from . import output
            self.output = output.Output()
        else:
            self.output = None

    def time(self):
        return self.t

    def step(self, duration):
        self.t += duration
        for cf in self.crazyflies:
            cf.integrate(duration, self.disturbanceSize, self.maxVel)
        for cf in self.crazyflies:
            cf.flip()

    # should be called "animate" or something
    # but called "sleep" for source-compatibility with real-robot scripts
    def sleep(self, duration):
        # operator // has unexpected (wrong ?) behavior for this calculation.
        ticks = math.floor((duration + self.sleepResidual) / self.dt)
        self.sleepResidual += duration - self.dt * ticks
        assert -1e-9 <= self.sleepResidual < self.dt

        for _ in range(int(ticks)):
            self.visualizer.update(self.t, self.crazyflies)
            if self.output:
                self.output.update(self.t, self.crazyflies)
            self.step(self.dt)

    # Mock for abstraction of rospy.Rate.sleep().
    def sleepForRate(self, rate):
        # TODO: account for rendering time, or is it worth the complexity?
        self.sleep(1.0 / rate)

    # Mock for abstraction of rospy.is_shutdown().
    def isShutdown(self):
        return False

    def addObserver(self, observer):
        self.observers.append(observer)


def collisionAvoidanceUpdateSetpoint(
    collisionParams, collisionState, mode, state, setState, otherCFs):
    """Modifies a setpoint based on firmware collision-avoidance algorithm.

    Main purpose is to hide the firmware's stabilizer_types.h types, because we
    prefer to work with cmath3d-based types.

    Args:
        collisionParams (firmware collisionAvoidanceParams_t): Collision
            avoidance algorithm parameters. Generally will remain constant.
        collisionState: (firmware collisionAvoidanceState_t): Opaque collision
            avoidance internal state. **Is modified in-place.** The same object
            should be passed to this function in repeated calls.
        mode (Crazyflie.MODE_* enum): The current flight mode.
        state (firmware traj_eval): The Crazyflie's currents state.
        setState (firmware traj_eval): The desired state generated by polynomial
            trajectory, user low-level commands, etc.
        otherCFs (array of Crazyflie): The other Crazyflie objects in the swarm.

    Returns:
        newSetState (firmware traj_eval): A new desired state that attempts to
            remain close to setState input while ensuring collision avoidance.
    """

    # This is significantly faster than calling position() on all the other CFs:
    # 1.2 vs 1.8 seconds in test_collisionAvoidance.py::test_goToWithCA_random.
    nOthers = len(otherCFs)
    otherPositions = np.zeros((nOthers, 3), dtype=np.float32)
    for i, cf in enumerate(otherCFs):
        otherPositions[i][0] = cf.state.pos.x
        otherPositions[i][1] = cf.state.pos.y
        otherPositions[i][2] = cf.state.pos.z

    cmdState = firm.state_t()
    # Position and velocity are the only states collision avoidance observes.
    cmdState.position = firm.svec2vec(state.pos)
    cmdState.velocity = firm.svec2vec(state.vel)

    # Dummy - it accepts the input to match the API of SitAw, but it's unused.
    sensorData = firm.sensorData_t()

    setpoint = firm.setpoint_t()
    if mode == Crazyflie.MODE_IDLE:
        pass
    elif mode in (Crazyflie.MODE_HIGH_POLY, Crazyflie.MODE_LOW_FULLSTATE):
        setpoint.mode.x = firm.modeAbs
        setpoint.position = firm.svec2vec(setState.pos)
        setpoint.velocity = firm.svec2vec(setState.vel)
    elif mode == Crazyflie.MODE_LOW_POSITION:
        setpoint.mode.x = firm.modeAbs
        setpoint.position = firm.svec2vec(setState.pos)
    elif mode == Crazyflie.MODE_LOW_VELOCITY:
        setpoint.mode.x = firm.modeVelocity
        setpoint.velocity = firm.svec2vec(setState.vel)
    else:
        raise ValueError("Unknown flight mode.")

    firm.collisionAvoidanceUpdateSetpointWrap(
        collisionParams,
        collisionState,
        otherPositions.flatten(),
        setpoint,
        sensorData,
        cmdState)

    newSetState = firm.traj_eval_zero()
    newSetState.pos = firm.vec2svec(setpoint.position)
    newSetState.vel = firm.vec2svec(setpoint.velocity)
    newSetState.yaw = setState.yaw
    newSetState.omega.z = setState.omega[2]
    return newSetState


class Crazyflie:

    # Flight modes.
    MODE_IDLE = 0
    MODE_HIGH_POLY = 1
    MODE_LOW_FULLSTATE = 2
    MODE_LOW_POSITION = 3
    MODE_LOW_VELOCITY = 4


    def __init__(self, id, initialPosition, timeHelper):

        # Core.
        self.id = id
        self.groupMask = 0
        self.initialPosition = np.array(initialPosition)
        self.time = lambda: timeHelper.time()

        # Commander.
        self.mode = Crazyflie.MODE_IDLE
        self.planner = firm.planner()
        firm.plan_init(self.planner)
        self.trajectories = dict()
        self.setState = firm.traj_eval_zero()

        # State. Public np.array-returning getters below for physics state.
        self.state = firm.traj_eval_zero()
        self.state.pos = firm.mkvec(*initialPosition)
        self.state.vel = firm.vzero()
        self.state.acc = firm.vzero()
        self.state.yaw = 0.0
        self.state.omega = firm.vzero()
        self.ledRGB = (0.5, 0.5, 1)

        # Double-buffering: Ensure that all CFs observe the same world state
        # during an integration step, regardless of the order in which their
        # integrate() methods are called. flip() swaps front and back state.
        # See http://gameprogrammingpatterns.com/double-buffer.html for more
        # motivation.
        self.backState = firm.traj_eval(self.state)

        # For collision avoidance.
        self.otherCFs = []
        self.collisionAvoidanceParams = None
        self.collisionAvoidanceState = None

    def setGroupMask(self, groupMask):
        self.groupMask = groupMask

    def enableCollisionAvoidance(self, others, ellipsoidRadii, bboxMin=np.repeat(-np.inf, 3), bboxMax=np.repeat(np.inf, 3), horizonSecs=1.0, maxSpeed=2.0):
        self.otherCFs = [cf for cf in others if cf is not self]

        # TODO: Accept more of these from arguments.
        params = firm.collision_avoidance_params_t()
        params.ellipsoidRadii = firm.mkvec(*ellipsoidRadii)
        params.bboxMin = firm.mkvec(*bboxMin)
        params.bboxMax = firm.mkvec(*bboxMax)
        params.horizonSecs = horizonSecs
        params.maxSpeed = maxSpeed
        params.sidestepThreshold = 0.25
        params.voronoiProjectionTolerance = 1e-5
        params.voronoiProjectionMaxIters = 100
        self.collisionAvoidanceParams = params

        state = firm.collision_avoidance_state_t()
        state.lastFeasibleSetPosition = firm.mkvec(np.nan, np.nan, np.nan)
        self.collisionAvoidanceState = state

    def disableCollisionAvoidance(self):
        self.otherCFs = None
        self.collisionAvoidanceParams = None
        self.collisionAvoidanceState = None

    def takeoff(self, targetHeight, duration, groupMask = 0):
        if self._isGroup(groupMask):
            self.mode = Crazyflie.MODE_HIGH_POLY
            targetYaw = 0.0
            firm.plan_takeoff(self.planner,
                self.state.pos, self.state.yaw, targetHeight, targetYaw, duration, self.time())

    def land(self, targetHeight, duration, groupMask = 0):
        if self._isGroup(groupMask):
            self.mode = Crazyflie.MODE_HIGH_POLY
            targetYaw = 0.0
            firm.plan_land(self.planner,
                self.state.pos, self.state.yaw, targetHeight, targetYaw, duration, self.time())

    def stop(self, groupMask = 0):
        if self._isGroup(groupMask):
            self.mode = Crazyflie.MODE_IDLE
            firm.plan_stop(self.planner)

    def goTo(self, goal, yaw, duration, relative = False, groupMask = 0):
        if self._isGroup(groupMask):
            if self.mode != Crazyflie.MODE_HIGH_POLY:
                # We need to update to the latest firmware that has go_to_from.
                raise ValueError("goTo from low-level modes not yet supported.")
            self.mode = Crazyflie.MODE_HIGH_POLY
            firm.plan_go_to(self.planner, relative, firm.mkvec(*goal), yaw, duration, self.time())

    def uploadTrajectory(self, trajectoryId, pieceOffset, trajectory):
        traj = firm.piecewise_traj()
        traj.t_begin = 0
        traj.timescale = 1.0
        traj.shift = firm.mkvec(0, 0, 0)
        traj.n_pieces = len(trajectory.polynomials)
        traj.pieces = firm.malloc_poly4d(len(trajectory.polynomials))
        for i, poly in enumerate(trajectory.polynomials):
            piece = firm.pp_get_piece(traj, i)
            piece.duration = poly.duration
            for coef in range(0, 8):
                firm.poly4d_set(piece, 0, coef, poly.px.p[coef])
                firm.poly4d_set(piece, 1, coef, poly.py.p[coef])
                firm.poly4d_set(piece, 2, coef, poly.pz.p[coef])
                firm.poly4d_set(piece, 3, coef, poly.pyaw.p[coef])
        self.trajectories[trajectoryId] = traj

    def startTrajectory(self, trajectoryId, timescale = 1.0, reverse = False, relative = True, groupMask = 0):
        if self._isGroup(groupMask):
            self.mode = Crazyflie.MODE_HIGH_POLY
            traj = self.trajectories[trajectoryId]
            traj.t_begin = self.time()
            traj.timescale = timescale
            if relative:
                traj.shift = firm.vzero()
                if reverse:
                    traj_init = firm.piecewise_eval_reversed(traj, traj.t_begin)
                else:
                    traj_init = firm.piecewise_eval(traj, traj.t_begin)
                traj.shift = self.state.pos - traj_init.pos
            else:
                traj.shift = firm.vzero()
            firm.plan_start_trajectory(self.planner, traj, reverse)

    def notifySetpointsStop(self, remainValidMillisecs=100):
        # No-op - the real Crazyflie prioritizes streaming setpoints over
        # high-level commands. This tells it to stop doing that. We don't
        # simulate this behavior.
        pass

    def position(self):
        return np.array(self.state.pos)

    def getParam(self, name):
        print("WARNING: getParam not implemented in simulation!")

    def setParam(self, name, value):
        print("WARNING: setParam not implemented in simulation!")

    def setParams(self, params):
        print("WARNING: setParams not implemented in simulation!")

    # - this is a part of the param system on the real crazyflie,
    #   but we implement it in simulation too for debugging
    # - is a blocking command on real CFs, so may cause stability problems
    def setLEDColor(self, r, g, b):
        self.ledRGB = (r, g, b)

    # simulation only functions
    def yaw(self):
        return float(self.state.yaw)
    
    def velocity(self):
        return np.array(self.state.vel)

    def acceleration(self):
        return np.array(self.state.acc)

    def rpy(self):
        acc = self.acceleration()
        yaw = self.yaw()
        norm = np.linalg.norm(acc)
        if norm > 5.0:
            print("acc", acc)
        if norm < 1e-6:
            return (0.0, 0.0, yaw)
        else:
            thrust = acc + np.array([0, 0, 9.81])
            z_body = thrust / np.linalg.norm(thrust)
            x_world = np.array([math.cos(yaw), math.sin(yaw), 0])
            y_body = np.cross(z_body, x_world)
            x_body = np.cross(y_body, z_body)
            pitch = math.asin(-x_body[2])
            roll = math.atan2(y_body[2], z_body[2])
            return (roll, pitch, yaw)

    def rpyt2force(self, roll, pitch, yaw, thrust):
        R = Rotation.from_euler('xyz', [roll, pitch, yaw], degrees=True).as_matrix()  # TODO: Verify degrees vs radians

        return R@np.array([0, 0, thrust])

    def cmdFullState(self, pos, vel, acc, yaw, omega):
        self.mode = Crazyflie.MODE_LOW_FULLSTATE
        self.setState.pos = firm.mkvec(*pos)
        self.setState.vel = firm.mkvec(*vel)
        self.setState.acc = firm.mkvec(*acc)
        self.setState.yaw = yaw
        self.setState.omega = firm.mkvec(*omega)

    def cmdPosition(self, pos, yaw = 0):
        self.mode = Crazyflie.MODE_LOW_POSITION
        self.setState.pos = firm.mkvec(*pos)
        self.setState.yaw = yaw
        # TODO: should we set vel, acc, omega to zero, or rely on modes to not read them?

    def cmdVelocityWorld(self, vel, yawRate):
        self.mode = Crazyflie.MODE_LOW_VELOCITY
        self.setState.vel = firm.mkvec(*vel)
        self.setState.omega = firm.mkvec(0.0, 0.0, yawRate)
        # TODO: should we set pos, acc, yaw to zero, or rely on modes to not read them?

    def cmdVel(self, roll_d, pitch_d, yaw_rate_d, thrust_d, yaw=0., dt=2e-2, g=9.81, m=0.034, hover_throttle=34/64):
        force = self.rpyt2force(roll_d, pitch_d, yaw, thrust_d)
        force *= g/hover_throttle
        acc = (force-firm.mkvec(0, 0, g))/m

        vel = self.state.vel + dt*firm.mkvec(*acc)
        #print('force: ', force, 'acc: ', acc, 'vel: ', vel)
        self.cmdVelocityWorld(vel, yaw_rate_d)

    def cmdStop(self):
        # TODO: set mode to MODE_IDLE?
        pass

    def integrate(self, time, disturbanceSize, maxVel):
        if self.mode == Crazyflie.MODE_HIGH_POLY:
            self.setState = firm.plan_current_goal(self.planner, self.time())

        if self.collisionAvoidanceState is not None:
            setState = collisionAvoidanceUpdateSetpoint(
                self.collisionAvoidanceParams,
                self.collisionAvoidanceState,
                self.mode,
                self.state,
                self.setState,
                self.otherCFs,
            )
        else:
            setState = firm.traj_eval(self.setState)

        if self.mode == Crazyflie.MODE_IDLE:
            return

        if self.mode in (Crazyflie.MODE_HIGH_POLY, Crazyflie.MODE_LOW_FULLSTATE, Crazyflie.MODE_LOW_POSITION):
            velocity = (setState.pos - self.state.pos) / time
        elif self.mode == Crazyflie.MODE_LOW_VELOCITY:
            velocity = setState.vel
        else:
            raise ValueError("Unknown flight mode.")

        # Limit velocity for realism.
        # Note: This will result in the state having a different velocity than
        # the setState in HIGH_POLY and LOW_FULLSTATE modes even when no
        # clamping occurs, because we are essentially getting rid of the
        # feedforward commands. We assume this is not a problem.

        velocity = firm.vclampnorm(velocity, maxVel)

        disturbance = disturbanceSize * np.random.normal(size=3)
        velocity = velocity + firm.mkvec(*disturbance)
        self.backState = firm.traj_eval(setState)
        self.backState.pos = self.state.pos + time * velocity
        self.backState.vel = velocity

        if self.mode == Crazyflie.MODE_LOW_POSITION:
            yawRate = (setState.yaw - self.state.yaw) / time
            self.backState.yaw = setState.yaw
            self.backState.omega = firm.mkvec(0.0, 0.0, yawRate)
        elif self.mode == Crazyflie.MODE_LOW_VELOCITY:
            # Omega is already set.
            self.backState.yaw += time * self.setState.omega.z

        # In HIGH_POLY and LOW_FULLSTATE, yaw and omega are already specified
        # in setState and have been copied.

    def flip(self):
        # Swap double-buffered state. Called at the end of the tick update,
        # after *all* CFs' integrate() methods have been called.
        self.state, self.backState = self.backState, self.state

    # "private" methods
    def _isGroup(self, groupMask):
        return groupMask == 0 or (self.groupMask & groupMask) > 0


class CrazyflieServer:
    def __init__(self, timeHelper, crazyflies_yaml="../launch/crazyflies.yaml"):
        """Initialize the server.

        Args:
            timeHelper (TimeHelper): TimeHelper instance.
            crazyflies_yaml (str): If ends in ".yaml", interpret as a path and load
                from file. Otherwise, interpret as YAML string and parse
                directly from string.
        """
        if crazyflies_yaml.endswith(".yaml"):
            with open(crazyflies_yaml, 'r') as ymlfile:
                cfg = yaml.safe_load(ymlfile)
        else:
            cfg = yaml.safe_load(crazyflies_yaml)

        self.crazyflies = []
        self.crazyfliesById = dict()
        for crazyflie in cfg["crazyflies"]:
            id = int(crazyflie["id"])
            initialPosition = crazyflie["initialPosition"]
            cf = Crazyflie(id, initialPosition, timeHelper)
            self.crazyflies.append(cf)
            self.crazyfliesById[id] = cf

        self.timeHelper = timeHelper
        self.timeHelper.crazyflies = self.crazyflies

    def emergency(self):
        print("WARNING: emergency not implemented in simulation!")

    def takeoff(self, targetHeight, duration, groupMask = 0):
        for crazyflie in self.crazyflies:
            crazyflie.takeoff(targetHeight, duration, groupMask)

    def land(self, targetHeight, duration, groupMask = 0):
        for crazyflie in self.crazyflies:
            crazyflie.land(targetHeight, duration, groupMask)

    def stop(self, groupMask = 0):
        for crazyflie in self.crazyflies:
            crazyflie.stop(groupMask)

    def goTo(self, goal, yaw, duration, groupMask = 0):
        for crazyflie in self.crazyflies:
            crazyflie.goTo(goal, yaw, duration, relative=True, groupMask=groupMask)

    def startTrajectory(self, trajectoryId, timescale = 1.0, reverse = False, relative = True, groupMask = 0):
        for crazyflie in self.crazyflies:
            crazyflie.startTrajectory(trajectoryId, timescale, reverse, relative, groupMask)

    def setParam(self, name, value):
        print("WARNING: setParam not implemented in simulation!")
