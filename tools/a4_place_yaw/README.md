# A4 place/yaw boards

이 폴더는 사람이 읽는 A4 좌표판과 로봇이 읽는 JSON 좌표 계약을 함께 생성한다.
전체 좌표 변환, grasp profile, 안전·품질 gate와 생산물 저장 규칙의 정본은 [결정론적 데이터팩토리 계획과 계약](../../docs/data-factory.md)이다.

## Generate

기본 출력 기준 위치는 이 스크립트가 있는 폴더다. 다른 위치는 `--output-dir`로 지정한다. 생성물은 기준 위치 아래의 `json/`, `pdf/`, `svg/`에 형식별로 저장된다.

```bash
python3 generate_place_yaw_a4.py \
  --yaw-deg 0 15 30 45 60 75 90 \
  --place-id PLACE_A \
  --pdf
```

기본 구성은 A4 가로, 5×3 grid, 35 mm 간격이다. 인쇄할 때 실제 크기/100%를 사용하고 페이지 맞춤을 끈다. 출력 후 100 mm scale bar가 실제 10 cm인지 확인한다.

각 JSON `grid_points[].job_pose`가 데이터팩토리에 넣는 `(place_id, yaw_deg, x_mm, y_mm)`이며, `x_mm/y_mm`는 종이에 표시된 `(u,v)`와 같다.
같은 `place_id`·페이지·격자·기준점 계열의 yaw 시트는 동일한 `a4_family_digest`를 가진다.
