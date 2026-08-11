# Third-party components

This repository links FAIRINO's ROS 2 repository as a submodule, applies a local patch, and includes an FR5-only robot-description subset for the current MoveIt configuration.

- [FAIR-INNOVATION/frcobot_ros2](https://github.com/FAIR-INNOVATION/frcobot_ros2): ROS 2 packages referenced by `src/frcobot_ros2` and modified at setup time by `patches/frcobot_ros2.patch`. The FR5 meshes and URDF under `src/fairino_description` are a reduced derivative of this upstream material. Its package manifests currently contain unresolved `TODO` license declarations; confirm redistribution terms before granting reuse rights.
- [FAIR-INNOVATION/fairino-cpp-sdk](https://github.com/FAIR-INNOVATION/fairino-cpp-sdk): `libfairino` headers and Linux shared library. The upstream repository declares Apache-2.0.
- [DH-Robotics PGEA-100-40](https://en.dh-robotics.com/product/pgea): the gripper CAD mesh under `src/fairino_description/meshes/gripper` is included for the current robot description. Confirm the CAD file's redistribution terms with its original provider before granting reuse rights.

The top-level Apache License 2.0 applies to repository-authored scripts, tools, tests, configuration, and documentation. It does not relicense the third-party submodule, derived robot-description files, vendor patch context, or CAD meshes listed above; those remain subject to their respective owners' terms.
