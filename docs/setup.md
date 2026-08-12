# 설치와 노트북 이식

## 목적

새 Ubuntu 수집 노트북과 별도 NVIDIA 학습 PC를 재현 가능하게 준비한다.

## 전제 조건

- Ubuntu 24.04와 ROS 2 Jazzy
- FR5 제어기와 통신 가능한 유선 LAN
- RealSense 1대 이상
- NVIDIA 학습을 할 경우 호환 드라이버. CUDA Python 패키지는 시스템 Python이 아니라 `.venv`에 설치된다.

## 수집 노트북 설치

```bash
git clone --recurse-submodules https://github.com/hasemu1211/fr5-lerobot-connector.git
cd fr5-lerobot-connector
git submodule update --init --recursive
scripts/setup_notebook.sh
```

스크립트는 다음만 수행한다.

1. ROS 2 Jazzy 존재 확인
2. colcon·rosdep·FFmpeg·RealSense ROS 패키지 설치
3. pinned FAIRINO submodule에서 필요한 hardware/message package만 sparse checkout하고 `patches/frcobot_ros2.patch`를 idempotent 적용
4. `.venv` 생성, CPU PyTorch와 LeRobot 0.6.1 `dataset` 기능 설치
5. rosdep 설치와 colcon 빌드
6. `config/fr5.env.example`을 `config/fr5.env`로 복사
7. 오프라인 사전점검

수집 노트북에는 SmolVLA·CUDA 패키지를 설치하지 않는다. 낮은 사양의 노트북에서도 ROS 수집과 LeRobot v3 저장에 필요한 패키지만 준비한다. 오프라인 사전점검에는 Python 문법, 리코더 단위 테스트, LeRobot 이미지 설정 검사가 포함된다.

비밀번호는 저장하지 않으며 `sudo`가 직접 요청한다.

## 장비 설정

`config/fr5.env`:

```bash
export ROS_DOMAIN_ID=58
export FR5_CONTROLLER_IP=192.168.58.2
export REALSENSE_NAMESPACE=camera
export REALSENSE_ROLE=up
export FR5_COLLECTION_FPS=30
# 필요할 때만 실제 by-id 경로 지정
# export UVC_DEVICE=/dev/v4l/by-id/usb-...-video-index0
```

### 로봇 제어망과 Wi-Fi

FR5 제어기는 기본 `192.168.58.2`이고 수집 PC의 전용 유선 NIC는 `192.168.58.100/24`를 사용한다. 인터넷/Wi-Fi가 `192.168.11.x`여도 서로 다른 subnet이므로 함께 사용할 수 있다. 로봇 NIC에는 default gateway를 두지 않아 인터넷 경로를 빼앗지 않게 한다.

```bash
ip -brief address
ip route get 192.168.58.2
# 정상 예: 192.168.58.2 dev <robot-nic> src 192.168.58.100
```

유선 NIC에 경로가 없을 때만 NetworkManager 연결을 만든다. `<robot-nic>`은 노트북에서 `ip -brief address`로 확인한 이름이다.

```bash
sudo nmcli connection add type ethernet ifname <robot-nic> con-name fr5-control \
  ipv4.method manual ipv4.addresses 192.168.58.100/24 \
  ipv4.never-default yes ipv6.method disabled
sudo nmcli connection up fr5-control
ping -c 3 192.168.58.2
```

인터페이스 이름은 USB 어댑터와 노트북마다 달라지므로 저장소 설정에 고정하지 않는다. `scripts/preflight_collection.sh --live`는 실제 로봇 route의 인터페이스와 source IP를 출력하고 ping 실패 시 중단한다.

RealSense는 `/dev/videoN`을 고정하지 않는다. `scripts/start_realsense_camera.sh`가 serial을 찾으며, 여러 대일 때만 카메라별 serial을 지정한다.

```bash
REALSENSE_ROLE=up    REALSENSE_SERIAL=<serial-1> scripts/start_realsense_camera.sh
REALSENSE_ROLE=side  REALSENSE_SERIAL=<serial-2> scripts/start_realsense_camera.sh
# 또는 손목 카메라
REALSENSE_ROLE=wrist REALSENSE_SERIAL=<serial-2> scripts/start_realsense_camera.sh
```

생성 토픽은 각각 `/camera/up/color/image_raw`, `/camera/side/color/image_raw`, `/camera/wrist/color/image_raw`다.

RGB만 기록할 때 `REALSENSE_ENABLE_SYNC=false`가 기본이다. 이는 한 RealSense 내부의 depth/color frame sync 옵션이며, 서로 다른 두 카메라의 동기화 기능이 아니다. 두 카메라와 로봇의 정합은 각 ROS header timestamp를 이용해 리코더가 검사한다.

`scripts/start_realsense_camera.sh`는 color publisher를 먼저 끈 상태에서 QoS와 bounded queue를 설정한 뒤 stream을 켠다. 카메라·USB 경로·노트북이 바뀔 때마다 다음 명령으로 source FPS, frame gap, transport age를 측정한다.

```bash
.venv/bin/python tools/measure_ros_topic_age.py \
  --duration 30 --reliable-image \
  --image-qos-depth 10 \
  --image /camera/up/color/image_raw \
  --metadata /camera/up/color/metadata
```

시간 오프셋은 설치 과정에서 생성·적용하지 않는다. 기본값은 0 ms이며 외부에서 독립 측정된 값만 명시적으로 적용한다.

### RealSense + 다른 제조사 카메라

두 번째 카메라는 RealSense일 필요가 없다. 일반 V4L2/UVC 장치는 공식 ROS `usb_cam`으로 실행한다.

```bash
UVC_ROLE=side scripts/start_uvc_camera.sh
# 자동 선택이 불가능하거나 여러 UVC가 있으면 stable by-id를 명시한다.
UVC_DEVICE=/dev/v4l/by-id/usb-...-video-index0 \
  UVC_ROLE=wrist scripts/start_uvc_camera.sh
```

launcher는 `/dev/videoN`을 설정에 저장하지 않고 `by-id`에서 non-RealSense `video-index0`를 선택한 뒤 실제 device를 resolve한다. 기본 토픽은 `/camera/side/color/image_raw`, 기본 형식은 RGB 640×480, 기본 FPS는 `FR5_COLLECTION_FPS`의 30이다. pinned `usb_cam`의 `mmap` 경로는 V4L2 buffer timestamp를 `Image.header.stamp`로 발행한다. Jazzy binary 0.8.1의 epoch microsecond 변환 결함을 피하려고 upstream 수정 commit `ee0a2f7`을 submodule로 빌드한다.

다른 제조사 전용 ROS 드라이버를 사용해도 아래 입력 계약은 같다.

- 메시지 형식: `sensor_msgs/msg/Image` (`CompressedImage`는 직접 입력 불가)
- 영상: RGB로 변환 가능한 3채널, 640x480, 기본 30fps. target 정합 오차와 반복률을 품질 보고서에 기록
- timestamp: 수신 PC의 ROS clock과 같은 시간축
- 장치 선택: `/dev/videoN` 대신 serial, `/dev/v4l/by-id`, 또는 udev 고정 이름

UVC 토픽 age는 metadata 인자 없이 측정한다.

```bash
.venv/bin/python tools/measure_ros_topic_age.py \
  --duration 30 --reliable-image --image-qos-depth 10 \
  --image /camera/side/color/image_raw
```

`scripts/preflight_collection.sh --live`도 선택된 각 카메라를 5초 측정해 source rate, 음수 timestamp age, age p95를 수집 시작 전에 강제 검사한다. 이 검사는 시작 전 검증이며 recorder loop에는 추가 작업을 넣지 않는다.

## 수집 노트북 확인

```bash
scripts/preflight_collection.sh
source .venv/bin/activate
python - <<'PY'
import torch
print(torch.__version__, torch.cuda.is_available())
PY
```

`CUDA=False`가 정상이다. 수집 노트북은 CPU에서 녹화하고 episode 종료 후 영상을 인코딩한다.

## 학습 PC 설치

학습 PC에는 NVIDIA 드라이버를 먼저 설치한 뒤 별도로 실행한다.

```bash
git clone --recurse-submodules https://github.com/hasemu1211/fr5-lerobot-connector.git
cd fr5-lerobot-connector
# PyTorch가 별도 index를 요구하는 환경에서만 지정한다.
# export PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cu130
scripts/setup_training.sh
```

스크립트는 `.venv`에 GPU PyTorch와 `lerobot[dataset,smolvla]==0.6.1`을 설치하고 CUDA·SmolVLA CLI를 검사한다. 이미 올바른 CUDA PyTorch가 있으면 다시 설치하지 않는다.

설치 확인:

```bash
scripts/train_smolvla.sh --check-env
scripts/evaluate_smolvla.sh --check-env
```

ROS 2의 `rclpy`는 시스템 ROS에서, LeRobot·PyTorch는 프로젝트 `.venv`에서 가져온다. 따라서 직접 실행할 때는 항상 다음 순서를 사용한다. `scripts/collect.sh`는 이 순서를 내부에서 수행한다.

```bash
source /opt/ros/jazzy/setup.bash
source install/setup.bash
source .venv/bin/activate
python -c 'import rclpy, lerobot, torch; print(lerobot.__version__, torch.__version__)'
```

데이터 이동 시 데이터셋 디렉터리 전체(`data/`, `meta/`, `videos/`)를 복사하고 내부 파일만 골라 복사하지 않는다. 복사 후 학습 PC에서 validator를 다시 실행한다.
