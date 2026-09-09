# Native dependency patches

`frcobot_ros2.patch` applies to the project's pinned ROS driver submodule.
Normal setup continues to use that patch and the existing SDK binary.

`fairino-cpp-sdk-2.3.7.patch` is an **unqualified, opt-in SDK candidate** against
[FAIRINO's v2.3.7-3.9.7 source](https://github.com/FAIR-INNOVATION/fairino-cpp-sdk/tree/0553c35d760a4e76c9b8d2fc0208ca83e6d731cd).
It reuses the CNDE receiver and adds a coherent, nonblocking snapshot getter.
Receipt time and local publication identity do not prove acquisition age,
command completion or physical safety. Legacy getters are not made coherent.
Active receive configuration changes return BUSY; stopped changes invalidate
the snapshot. Native FR5 consumption and physical qualification are separate.

The reproducible opt-in test exports the pinned commit from a local SDK clone,
applies the patch in temporary storage and builds/tests it without installation
or device calls. It verifies actual library selection and zero network syscalls:

```sh
FR5_FAIRINO_SDK_REPO=/path/to/fairino-cpp-sdk direnv exec . \
  python3 -m unittest tests.data_factory.rollout.test_sdk_snapshot -v
```

No download or SDK build is added to ordinary unit discovery. Upstream SDK source
and patch context are covered by [Apache-2.0](fairino-sdk-LICENSE); the patch
identifies the modified files. No manufacturer binary or dataset is included.
