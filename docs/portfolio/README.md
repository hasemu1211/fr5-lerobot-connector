# Robot Learning Data Engine · 포트폴리오

로봇 데이터의 수집부터 정책 비교까지, 실제 시연과 기술 원리로 살펴보는 포트폴리오이다. FAIRINO FR5를 구현·검증 플랫폼으로 사용한다.

## 포트폴리오 다운로드

**[FR5-Portfolio.html 다운로드 · 38.2MB](https://github.com/hasemu1211/fr5-lerobot-connector/releases/download/portfolio-2026-09-08/FR5-Portfolio.html)**

다운로드한 파일을 데스크톱 Chrome에서 연다. 영상 재생, 표본 선택, 정책 비교와 근거 열람을 파일 하나에서 사용할 수 있다. 설치·압축 해제·서버는 필요하지 않으며 외부 문헌 링크만 인터넷을 사용한다.

- **시연과 수집 원리:** Pick·Pick & Place, 작업 조건과 센서 동기화
- **데이터와 모방학습:** 영상·상태·목표 동작, 데이터 선별과 SmolVLA
- **시스템과 비교 결과:** 폐루프 구조, 같은 조건의 정책 비교와 원본 근거

GitHub에서 바로 읽을 기술 문서는 [시스템 아키텍처](../architecture.md), [데이터셋 품질](../dataset-quality.md), [학습과 평가](../training-and-evaluation.md)에 있다.

<details>
<summary>편집과 단일 파일 생성</summary>

## 단일 파일 만들기

저장소 루트에서 Python 3으로 실행한다. 추가 패키지는 필요하지 않다.

```sh
python3 docs/portfolio/export_single_file.py .agent-local/portfolio/FR5-Portfolio.html
```

이미지·동영상·글꼴·근거 페이지를 포함해 50MB 미만의 파일을 생성하고, 파일 크기와 SHA-256을 출력한다. 생성된 파일을 그대로 전달하면 된다. 수정은 이 폴더의 원본에 반영한 뒤 같은 명령으로 다시 생성한다.

## 편집 원본

| 위치 | 용도 |
| --- | --- |
| `index.html` 및 주제별 HTML | 본문과 화면 구성 |
| `*.drawio.svg`, `assets/` | 도해·그래프·영상·이미지·글꼴 |
| `sources/` | 실제 코드·데이터·보고서의 출처와 발췌 |
| `*.js`, `site.css` | 탐색·상호작용·화면 스타일 |
| `export_single_file.py` | 단일 HTML 생성 |

페이지 주소와 자산 경로는 상호작용과 내보내기에서 함께 사용한다. 파일 이동 전 소비 경로를 확인한다. 초안·스크린샷·생성본은 원본 폴더 밖에 보관한다. 외부 자산의 라이선스는 해당 자산과 함께 유지한다.

편집 판단은 [OpenSpec의 다섯 원칙](../../openspec/changes/establish-portfolio-proof-loop-intent/specs/portfolio-proof-loop/spec.md#requirement-portfolio-feedback-improves-the-reader-experience-while-preserving-core-value)을 따른다. 목적과 기술적 기여를 앞세우고 반복 설명을 줄이며, 각 주제에 적합한 매체를 선택한다.

작업명은 **Pick · Pick & Place**, 모듈명은 실제 아키텍처의 **Collection Operator · Recorder · Curator · Training Review · Policy Learning**을 일관되게 사용한다. 한국어는 목적과 원리를 설명하며, 원본 코드·데이터 식별자는 보존한다.

새 근거는 기존 `sources/` 연결에 반영한다. 설명용 예시, 실제 시연, 오프라인 정책 비교와 폐루프의 목표를 구분하고, 달성한 범위를 넘어 성능을 주장하지 않는다. 전달 전에는 단일 파일에서 바뀐 구간의 가독성·조작·근거 복귀를 직접 확인한다.

</details>
