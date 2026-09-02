# A4 place/yaw boards

이 폴더는 사람이 읽는 A4 좌표판과 로봇이 읽는 JSON 좌표 계약을 함께 생성한다.
전체 좌표 변환, grasp profile, 안전·품질 gate와 생산물 저장 규칙의 정본은 [결정론적 데이터팩토리 계획과 계약](../../docs/data-factory.md)이다.

## Generate

저장소 루트에서 실행한다. 기본 출력 기준 위치는 이 스크립트가 있는 폴더다. 다른 위치는 `--output-dir`로 지정한다. 생성물은 기준 위치 아래의 `json/`, `pdf/`, `svg/`에 형식별로 저장된다.

```bash
direnv exec . python3 tools/a4_place_yaw/generate_place_yaw_a4.py \
  --yaw-deg 0 15 30 45 60 75 90 \
  --place-id PLACE_A \
  --pdf
```

기본 구성은 A4 가로, 5×3 grid, 35 mm 간격이다. 인쇄할 때 실제 크기/100%를 사용하고 페이지 맞춤을 끈다. 출력 후 100 mm scale bar가 실제 10 cm인지 확인한다.

프린터가 100 mm 막대를 96 mm로 출력하고 배율 옵션도 제공하지 않으면 기존 PDF를 덮어쓰지 말고 다음처럼 보정본을 만든다.

```bash
direnv exec . python3 tools/a4_place_yaw/generate_place_yaw_a4.py \
  --yaw-deg 0 30 \
  --place-id PLACE_A \
  --measured-scale-mm 96 \
  --pdf
```

보정본은 `print_calibration/scale_bar_096_00mm/{json,pdf,svg}/`에만 생성되고 파일명과 종이 상단에도 `PRINTCAL_096_00MM`이 표시된다. 이 보정본의 100 mm 막대를 다시 측정해 100 mm임을 확인한 뒤 사용한다.

각 JSON `grid_points[].job_pose`가 데이터팩토리에 넣는 `(place_id, yaw_deg, x_mm, y_mm)`이며, `x_mm/y_mm`는 종이에 표시된 `(u,v)`와 같다.
같은 `place_id`·페이지·격자·기준점·인쇄 보정 계열의 yaw 시트는 동일한 `a4_family_digest`를 가진다.

## PLACE_A RED / PLACE_B BLUE zone 준비물

다음 명령은 기존 96→100 mm printcal 계산을 그대로 적용해 `zone_artifacts/`에 workspace-region JSON과 A4 가로 SVG/PDF 두 장을 만든다.

```bash
direnv exec . python3 tools/a4_place_yaw/generate_place_yaw_a4.py \
  --red-blue-zone \
  --measured-scale-mm 96 \
  --pdf
```

생성물은 다음과 같다.

- `a4_place_a_red_r002_printcal_096_00mm.pdf`: 작업영역 A에 놓는 전체 RED A4
- `a4_place_b_blue_r002_printcal_096_00mm.pdf`: 작업영역 B에 놓는 전체 BLUE A4
- `a4_place_a_red_place_b_blue_r002_printcal_096_00mm.json`: 각 `place_id`와 A4-local convex polygon을 묶는 좌표 계약

Web UI 자동 표본기는 이 polygon을 물체 footprint와 해당 frame의 보정 불확실성만큼 안쪽으로 침식한 뒤 5×3 층화 표본을 만든다. polygon 검증·안전 침식·표본 생성은 분리되어 있어 convex polygon 모양을 바꿔도 UI나 motion owner를 바꾸지 않는다.

이 파일들은 인쇄·배치 준비물일 뿐 motion 자격이 아니다. 각 A4를 해당 작업영역에 포개 고정하고 영속 region binding이 검증되기 전에는 학습 문장에 RED/BLUE를 넣지 않는다. PDF는 설치된 `svglib`/`reportlab`을 우선 사용하고, 없으면 시스템 `libreoffice` 변환기를 사용한다.
