![Crazyswarm ROS CI](https://github.com/USC-ACTLab/crazyswarm/workflows/Crazyswarm%20ROS%20CI/badge.svg)
![Sim-Only Conda CI](https://github.com/USC-ACTLab/crazyswarm/workflows/Sim-Only%20Conda%20CI/badge.svg)
[![Documentation Status](https://readthedocs.org/projects/crazyswarm/badge/?version=latest)](https://crazyswarm.readthedocs.io/en/latest/?badge=latest)

# Crazyswarm
A Large Nano-Quadcopter Swarm.

The documentation is available here: http://crazyswarm.readthedocs.io/en/latest/.

## Troubleshooting
Please start a [Discussion](https://github.com/USC-ACTLab/crazyswarm/discussions) for...

- Getting Crazyswarm to work with your hardware setup.
- Advice on how to use the [Crazyswarm Python API](https://crazyswarm.readthedocs.io/en/latest/api.html) to achieve your goals.
- Rough ideas for a new feature.

Please open an [Issue](https://github.com/USC-ACTLab/crazyswarm/issues) if you believe that fixing your problem will involve a **change in the Crazyswarm source code**, rather than your own configuration files. For example...

- Bug reports.
- New feature proposals with details.

## Installation instructions
The Crazyswarm package can be installed as described in the [documentation](https://crazyswarm.readthedocs.io/en/latest/installation.html). If you get an error when building for Python 3 compability, the build.sh file must be modified by adding the following option to the first catkin_make command: `-DPYTHON_EXECUTABLE=/usr/bin/python3`.

If you are using Ubuntu 20.04 and are having problems installing `gcc-arm-embedded`, try this [solution](https://askubuntu.com/questions/1243252/how-to-install-arm-none-eabi-gdb-on-ubuntu-20-04-lts-focal-fossa). 

