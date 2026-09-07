# FR5 로봇 수집·학습 시스템

FR5 로봇의 시연 수집, 다중 센서 기록, 데이터 관리와 모방학습을 소개하는 포트폴리오이다.

전달물은 `FR5-Portfolio.html` 파일 하나이다. 데스크톱 Chrome에서 열면 압축 해제·설치·서버 없이 이미지, 동영상, 화면 선택, 수치 비교와 근거 열람을 사용할 수 있다. 외부 문헌 링크만 인터넷을 사용한다.

이 폴더는 편집 원본이다. 평소에는 [index.html](index.html)을 열어 수정한 내용을 확인하고, 전달할 때 아래 명령으로 단일 파일을 생성한다. 생성된 HTML을 직접 편집하지 않는다.

```sh
python3 docs/portfolio/export_single_file.py .agent-local/portfolio/FR5-Portfolio.html
```

기존 파일을 같은 위치에 다시 생성하므로 배포본을 별도로 관리하거나 사본을 쌓지 않는다. Python 표준 라이브러리만 사용하며, 영상·이미지·글꼴과 근거 페이지를 포함한 결과가 50MB 이상이면 생성을 중단한다. 새 상호작용을 추가한 경우에는 생성된 파일에서도 해당 동작을 확인한다.

- [시스템 아키텍처](architecture.html): 모듈 구성과 데이터 흐름
- [Collection Operator](collection.html): 작업 조건, 반복 수집과 기록 구간
- [Recorder · Curator](data.html): 저장 관측, 시간 정렬과 영상 변환·검토
- [Policy Learning](learning.html): SmolVLA 학습 입력과 오프라인 비교

실제 저장 관측, FAKE 모드의 제품 화면과 오프라인 학습 결과는 각 매체의 캡션에서 구분한다. 근거 화면에는 원본 파일 식별자와 SHA-256, 발췌 범위를 함께 표시한다.

모듈 이름은 아키텍처와 본문에서 동일하게 사용한다: Collection Operator, OneJob, Motion Executor, Recorder, Dataset Validator, Curator, Selection, Video Transform, Training Review, Batch Review, NativeInspection, Policy Learning, Training Entrypoint, Split & Normalization. 한국어는 역할과 원리를 설명하는 보조 표현으로 사용하며 원본 코드·측정 자료의 식별자는 바꾸지 않는다.
