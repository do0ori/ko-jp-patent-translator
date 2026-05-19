# Plan: Streamlit Cloud 인프라 지표를 Google Sheets로 푸시

> **Revision history**
> - v1: 초안 (작성)
> - v2: codex 1차 리뷰 반영 — 주기 flush, runs 2-step write, contextvars 제거, 데이터 모델 보강, 1차 PR 범위 축소
> - v3: 사용자 직접 개정 — sampler/flusher 분리, lock 순서 1차안, `n_failed_chunks` 위치 확정 (runner), `running` 해석 약화 (stale row), bounded buffer 언급, 검증 시나리오 추가
> - v4: codex 2차 리뷰 반영 — `_SheetsSink` IO lock 명시, `MAX_BUFFER_ROWS` 폐기 정책 구체화, lock 획득 순서 문서화, 429 retry 카운팅 의미 정정, recursive split fallback 의 metrics 전달 명시, runner fail-fast 정책 결정, gspread find 컬럼 한정, psutil prime 처리, 작업 추정 9-12h 로 상향. **conditional go → go.**
> - v4.1: codex 3차 sanity check 반영 — finalize 시 `_sink_io_lock` 획득에 timeout(3.0s) + skip + warning 추가. **최종 go.**

## 0. 운영 전제 (명시)

- 솔로 사용자, 단일 세션, 동시 1건 번역만 발생한다고 가정.
- 다중 사용자 / 다중 탭 / 동시 번역은 1차 범위 밖. module-level global collector + lock 으로 충분.
- 민감정보 마스킹은 의도적으로 하지 않음 (개인 프로젝트, 문서 식별이 분석에 도움). 단 `error_msg`는 분석 편의로 `error_type` + `error_short`(앞 N자) 으로만 분리.

## 1. 배경 / 목적

- Streamlit Cloud 컨테이너는 트래픽 없을 때 sleep 되고 in-memory/로컬 파일 모두 휘발.
- 실시간 관찰 없이도 "어떤 문서를 몇 워커로 돌렸을 때 RAM 피크가 얼마였는지", "429 retry가 얼마나 났는지", "OOM으로 죽었는지"를 사후 분석 가능하게 한다.
- 2차 목표: `TRANSLATION_MAX_WORKERS` 튜닝 근거 데이터 축적 (현재 8 → 12/16 실험).
- **OOM 등으로 프로세스가 죽어도 직전 샘플이 남아 있어야 한다** — 이게 v1 대비 가장 큰 설계 변경.
- 단, `status=running` 으로 남은 row 는 "abnormal termination" 의 강한 증거가 아니라 **"finalize 미확인 / stale running row"** 로 해석한다. OOM 외에도 Sheets update 실패, Streamlit rerun, 배포 재시작 모두 같은 흔적을 남기기 때문. 정확한 원인 추정은 `samples` 의 마지막 `sampled_at` + Cloud 로그 + run duration 추정을 같이 봐야 한다.

## 2. Scope

### In-scope (1차 PR)
- 번역 1회 = `runs` 시트 row 1개 (시작 시 `running` 으로 append, 종료 시 update)
- `samples` 시트에 주기적 buffer flush (~30s 간격, 마지막 잔여분도 flush)
- retry / split fallback / API call 카운터를 명시적 `incr()` 로 수집
- `translating`, `building_doc` 두 phase만 정확히 기록 (parsing은 후순위)
- best-effort: Sheets 호출 실패해도 번역 결과에는 영향 없음

### Out-of-scope (후속 PR로 분리)
- Discord webhook 알림
- Sheets 차트 자동 셋업 (수동 1회면 충분)
- `parsing` phase 정확 측정 (현재 앱 구조상 버튼 클릭 전에 파싱이 끝나므로 모호함)
- 다중 사용자 / 다중 세션 동시 실행 대응
- 시트 retention 자동화 (월별 탭 분할 등) — 일단 단일 시트 운영, 커지면 후속

## 3. 데이터 모델

`run_id`(UUID4) 로 join.

### `runs` 시트

번역 시작 시점에 `status=running` 으로 1행 append. 종료 시점에 `run_id` 로 찾아 같은 행을 update.
종료 update 실패 케이스도 대비해 별도 `runs_events` 시트로 분리할지는 1차에서 미적용 (단일 row update 실패하면 row가 그대로 `running` 으로 남는 것을 "abnormal termination" 신호로 해석).

| 컬럼 | 타입 | 비고 |
|---|---|---|
| `run_id` | string | UUID4 |
| `started_at` | ISO8601 UTC | append 시점 |
| `ended_at` | ISO8601 UTC \| empty | running 동안 비움 |
| `duration_total_s` | float \| empty | |
| `duration_translate_s` | float \| empty | translating phase만 |
| `duration_build_doc_s` | float \| empty | building_doc phase만 |
| `doc_name` | string | 원본 파일명 그대로 (마스킹 X) |
| `file_size_bytes` | int | uploaded_file 크기 |
| `n_paragraphs` | int | parsed paragraphs 총수 |
| `n_images` | int | figure 요소 총수 |
| `n_chunks` | int | |
| `n_text_chunks` | int | |
| `n_figure_chunks` | int | |
| `chunk_size_max` | int | TEXT chunk 중 최대 paragraph 수 |
| `chunk_size_avg` | float | |
| `total_input_chars` | int | TEXT chunk 합산 (이미지 제외) |
| `total_output_chars` | int \| empty | 번역 결과 합산, 실패 시 비움 |
| `workers` | int | `TRANSLATION_MAX_WORKERS` |
| `model_name` | string | `gemini-2.5-flash` |
| `sample_interval_s` | float | `METRICS_SAMPLE_INTERVAL_S` |
| `peak_ram_mb` | float \| empty | samples 의 max |
| `avg_process_cpu_pct` | float \| empty | samples 평균 (단일 코어 기준 100%, 멀티 코어면 > 100% 가능) |
| `peak_process_threads` | int \| empty | |
| `n_text_api_calls` | int | 성공/실패 무관 attempt 합 |
| `n_image_api_calls` | int | |
| `n_429_errors` | int | 429를 받은 횟수 (retry 여부 무관) |
| `n_429_retries` | int | 실제로 retry 수행한 횟수 |
| `n_mismatch_errors` | int | ParagraphMismatchError 발생 횟수 |
| `n_mismatch_retries` | int | mismatch 후 실제 retry 수행 |
| `n_split_fallbacks` | int | split fallback 진입 횟수 |
| `n_failed_chunks` | int | 모든 retry/fallback 후에도 실패한 chunk 수 |
| `status` | enum | `running` / `ok` / `error` |
| `error_type` | string \| empty | `RuntimeError` 등 클래스명 |
| `error_short` | string \| empty | 방어적으로 `str(e)` 시도, 실패 시 `repr(e)[:500]`. 줄바꿈/탭은 공백 치환 후 500자 절단 |
| `app_version` | string | env `APP_VERSION` → git SHA → `"unknown"` 순으로 fallback |
| `n_dropped_samples` | int | sampler buffer cap 초과로 drop 한 sample 수 (정상 시 0) |
| `was_append_only` | bool | `running` row append 자체가 실패해 finalize 가 append-only 로 종료 row 를 새로 쓴 경우 true |

### `samples` 시트

주기적으로 buffer 를 batch append. row 순서 꼬임 대비해 절대 timestamp 컬럼 포함.

| 컬럼 | 타입 |
|---|---|
| `run_id` | string |
| `sampled_at` | ISO8601 UTC |
| `t_offset_s` | float (run started_at 기준) |
| `ram_mb` | float (`process.memory_info().rss / 1024**2`) |
| `process_cpu_pct` | float (`process.cpu_percent(interval=None)`, 워밍업 1회 후) |
| `process_threads` | int (`process.num_threads()`) |
| `phase` | enum (`translating` / `building_doc`) |

## 4. 아키텍처 / 데이터 흐름

```
[run_translation() in app.py]
        |
        v
[MetricsCollector(run_id, sheet_client)]
[collector.start()]
   ├─> 백그라운드 sampler thread (daemon)
   │     매 sample_interval_s 마다 psutil 읽어 buffer 에만 append
   │     (절대 직접 Sheets 호출 X — Sheets 응답 지연이 sampling을 막으면 안 됨)
   │
   ├─> 백그라운드 flusher thread (daemon, 별도)
   │     매 FLUSH_INTERVAL_S 또는 buffer >= FLUSH_BATCH_SIZE 시 깨어남
   │     buffer swap 후 samples 시트로 batch append
   │     Sheets 호출 실패 시 swapped batch만 폐기 (bounded — 무한 보존 안 함, 다음 cycle 정상 진행)
   │
   └─> 번역 시작 직전 runs 시트에 status=running row append (1회)
        |
   [translate_chunks_parallel(chunks, ..., metrics_collector=collector)]   # 명시적 인자 전달
        ├─> worker thread 내부에서 translation.py 호출
        │     retry / split / api_call 시점에 collector.incr(...)
        │     (collector는 threading.Lock 으로 보호된 dict 카운터)
        |
   [build_doc_from_translated_chunks(...)]
        |
        v
[finally: collector.stop_and_finalize(status, error)]
        ├─> sampler/flusher Event.set, 각각 join (timeout 1.0s)
        ├─> 남은 buffer 강제 flush 1회 (lock 짧게 잡고 swap → swap 본을 락 밖에서 push)
        └─> runs 시트 row update (run_id 로 찾아서 ended_at, duration*, peak_*, n_* 등 update)
```

### 핵심 결정

- **collector 보유 방식: app.py 에서 인스턴스 생성 → `translate_chunks_parallel` 인자로 명시 전달**
  - contextvars 는 ThreadPoolExecutor worker thread 로 자동 전파되지 않으므로 사용하지 않음.
  - `_translate_single_chunk` 시그니처에 `metrics: MetricsCollector | None = None` 추가.
  - `translation.py` 의 `retry_with_delay` / `translate_text_with_gemini` 가 `metrics` 를 받아 `incr()` 호출.
  - **recursive split fallback (`translate_text_with_gemini` 내부 left/right 재귀 호출) 에도 동일 `metrics` 인자 명시 전달 필수.** 누락 시 split 이후 API call / 429 / mismatch 가 전부 누락됨.
- **collector가 None 일 때 no-op**: `NullMetricsCollector` 가 default. 반드시 전체 인터페이스 (`start`, `stop_and_finalize`, `incr`, `set_phase`, `record`, `record_failed_chunk`) 를 동일 시그니처로 구현해야 호출부에 None 체크가 없어진다.
- **flush 주기**: `FLUSH_INTERVAL_S = 30`, `FLUSH_BATCH_SIZE = 30` 중 먼저 도달하는 쪽. 1.5s interval 기준 30초면 ~20 rows라 사실상 시간 조건이 먼저 발화.
- **buffer cap 정책 (`MAX_BUFFER_ROWS = 2000`)**: sampler 가 buffer 에 append 하기 직전 길이 확인 → cap 초과 시 **drop-oldest** (head 에서 가장 오래된 1행 제거 후 append). drop 발생 시 collector 내부 카운터 `n_dropped_samples` 증가, 종료 시 `runs.n_dropped_samples` 컬럼 (※ §3 데이터 모델에 추가 필요) 으로 기록. 의미: Sheets 가 장시간 hang / flusher 사망 같은 비정상 상황에서도 메모리는 bounded.
- **lock 순서 규칙 (deadlock 회피 절대 순서)**:
  1. `_counter_lock` (incr / counters snapshot 용, in-memory dict)
  2. `_buffer_lock` (samples buffer / phase 읽기·쓰기 용, in-memory list/scalar)
  3. `_sink_io_lock` (`_SheetsSink` 내부, gspread 호출 직렬화 용)
  - 한 함수가 동시에 둘 이상 잡지 않는 것이 기본. 잡아야 할 경우 위 순서로만 acquire.
  - lock 1, 2 안에서는 **IO 호출 금지** (lock 들고 Sheets 호출 → 다른 thread 가 같은 lock 으로 막힘).
  - sampler/flusher 둘 다 `incr` 와 buffer swap 시 짧게 잡고 즉시 해제. Sheets 호출은 반드시 lock 밖.
- **`_SheetsSink` IO lock**: `_SheetsSink` 가 내부에서 `_sink_io_lock` 보유. flusher 의 주기 flush 와 finalize 의 final flush + runs row update 가 동시에 같은 gspread client 를 만지지 않도록 모든 sink 호출(`append_samples`, `append_run`, `update_run`) 진입부에서 lock 획득 후 호출, 종료 시 해제. **finalize 는 `stop_and_finalize()` 안에서 (a) flusher Event.set → (b) `flusher.join(timeout=1.0s)` → (c) `_sink_io_lock.acquire(timeout=3.0s)` 시도. 잡으면 final flush + update, 실패하면 (= flusher 가 Sheets IO 에서 hang 중) logging.warning 후 skip** 하여 사용자 페이지 전환을 무한정 막지 않음. skip 된 경우 runs row 는 `running` 으로 남고 `n_dropped_samples` 에는 영향 없음.
- **`n_failed_chunks` 위치 확정**: `translate_chunks_parallel` 에서 `future.result()` 호출을 try/except 로 감싸 exception 발생 시 `collector.record_failed_chunk()` 호출. translation.py 내부에서는 **카운팅 절대 금지** (중복 방지).
- **Runner fail-fast 정책 확정**: 1차에서는 첫 chunk 실패 시 즉시 raise 하던 기존 동작 유지. 단, raise 전에 `record_failed_chunk()` 1회 호출. 다른 in-flight future 는 `with ThreadPoolExecutor` context 종료 시 자연스럽게 drain 되며, 그 결과는 무시 (실패한 run 의 partial 결과는 사용하지 않음). drain 중 발생하는 추가 실패는 카운트하지 않음.
- **429 retry 카운팅 의미**: `n_429_errors` 는 429 catch 시점에 무조건 +1. `n_429_retries` 는 **catch 후 다음 attempt 가 실제로 실행될 때만** +1 (마지막 attempt 의 429 는 retries 에 포함하지 않음). 동일 규칙을 `n_mismatch_errors` / `n_mismatch_retries` 에도 적용.
- **runs row update 방법**: `gspread` `find` 를 **`run_id` 컬럼 범위로 한정** 해서 호출 (header 매칭 회피). row 가 많아지면 느려질 수 있으므로 시작 시 append 응답에서 row 번호를 받아 collector 인스턴스에 캐시 → finalize 시 캐시된 row 번호로 직접 `update_cells` 호출. `find` 는 캐시 무효 시의 fallback.
- **`running` append 실패 시 finalize 동작**: `running` 행 append 자체가 실패하면 collector 가 `_running_row = None` 으로 보관. finalize 시 `_running_row is None` 이면 **append-only 로 종료 상태 row 를 새로 1행 append** (status, error_*, ended_at, durations, peak_* 모두 포함). 이 경로는 `runs.was_append_only = True` 플래그로 구분.
- **sampler/flusher 내부 예외 격리**: 두 thread 함수 모두 최외곽 `while not stop.is_set():` 루프 안에 `try/except Exception: logging.exception(...)` 를 둬서 1회성 예외로 thread 가 죽지 않도록.

## 5. 인증 / Secrets

- `st.secrets["gcp_service_account"]` — GCP service account JSON (TOML 형태).
- `st.secrets["metrics_sheet_id"]` — 대상 스프레드시트 ID.
- 스프레드시트 공유: service account 이메일에 Editor 권한.
- 로컬 dev: `.streamlit/secrets.toml`. `.gitignore` 에 이미 포함되어 있는지 1차 PR 시작 시 1회 확인.
- `APP_VERSION` env: Streamlit Cloud 에서는 수동 secret 으로 주입 가능. 미설정 시 `subprocess` 로 `git rev-parse --short HEAD` 시도, 실패하면 `"unknown"`.

## 6. 구현 변경점

### 신규 파일
- `utils/metrics.py`
  - `MetricsCollector`: start / stop_and_finalize / incr(key, n=1) / set_phase / record(run_meta_fields)
  - `NullMetricsCollector`: 모든 메서드 no-op (default)
  - `_SheetsSink`: 시트 클라이언트 + append/update 메서드, 실패 시 logging.exception 후 swallow
  - 내부 lock 보호 카운터 dict, samples buffer (list)
  - psutil import 는 함수 안에서 try/except (없을 때 sampler 자체를 비활성)

### 수정 파일
- `app.py`
  - `run_translation()` 진입 시 collector 생성, `collector.start()`, status=running row append
  - `translate_chunks_parallel(..., metrics_collector=collector)` 호출
  - phase 전환: 번역 시작 직전 `set_phase("translating")`, build_doc 직전 `set_phase("building_doc")`
  - `try/except/finally` 로 감싸 예외 시 `error_type/error_short` 채워 `stop_and_finalize`
- `utils/translation_runner.py`
  - `translate_chunks_parallel(..., metrics_collector=None)` 시그니처 추가
  - `_translate_single_chunk` 에 collector 전달 (api_call 카운팅은 runner 가 아니라 translation.py 쪽에서 함)
  - `future.result()` 호출을 try/except 로 감싸 chunk 단위 최종 실패 시 `metrics_collector.record_failed_chunk()` 호출 후 다음 chunk 진행 여부 결정 (1차에서는 단일 chunk 실패 = 전체 실패로 raise 하는 기존 동작 유지)
- `utils/translation.py`
  - `_get_collector()` 모듈 함수 또는 인자 전달 — **인자 전달 채택** (테스트성, 명시성)
  - `retry_with_delay` / `_translate_text_batch_with_retry` / `translate_image_with_gemini` / `translate_text_with_gemini` 시그니처에 `metrics` 추가 (default `NullMetricsCollector()`)
  - `translate_text_with_gemini` 의 **recursive split fallback (`translate_text_with_gemini(paragraphs[:mid], ..., metrics=metrics)` / `paragraphs[mid:]`) 에도 `metrics` 명시 전달** — 빠뜨리면 split 이후 카운트 누락
  - 분기점에서 `incr` 호출:
    - 매 attempt 진입 직전: `n_text_api_calls` 또는 `n_image_api_calls`
    - 429 catch 시점: `n_429_errors` +1 (무조건)
    - 429 sleep 종료 후 **다음 attempt 진입이 실제로 일어날 때만**: `n_429_retries` +1 (마지막 attempt 의 429 는 retries 에 포함 X)
    - ParagraphMismatchError catch: `n_mismatch_errors`. 다음 attempt 가 실제로 일어날 때만: `n_mismatch_retries`
    - split fallback 진입: `n_split_fallbacks` +1
    - **`n_failed_chunks` 는 절대 translation.py 에서 incr 하지 않음.** runner 쪽 단일 지점에서만 기록.
- `utils/config.py`
  - `METRICS_ENABLED` (default False, env/secrets 로 토글)
  - `METRICS_SAMPLE_INTERVAL_S = 1.5`
  - `METRICS_FLUSH_INTERVAL_S = 30`
  - `METRICS_FLUSH_BATCH_SIZE = 30`
- `requirements.txt`
  - `psutil`, `gspread`, `google-auth` 추가
- `.streamlit/secrets.example.toml` 신규

### 미수정
- `chunker.py`, `docx_parser.py`: 메트릭 무관.
- 단, app.py 에서 `n_paragraphs`, `n_images`, `chunk_size_*`, `total_input_chars` 는 이미 파싱된 결과로 계산 가능하므로 collector 외부에서 계산해 `record()` 로 넘김.

## 7. 엣지 케이스 / 리스크

| 상황 | 처리 |
|---|---|
| psutil 없음 / metric 권한 부족 | sampler 비활성, runs row 는 그대로 기록 (peak_* 비움) |
| Sheets 호출 실패 (network/429/auth) | exception swallow + logging. 번역에는 영향 X |
| service account 미설정 / `METRICS_ENABLED=False` | NullMetricsCollector 로 fall through, 코드 경로 동일 |
| 번역 도중 예외 | finally 에서 status=`error`, error_type/error_short 채워 update |
| OOM 으로 프로세스 kill | 직전 flush 분까지 samples 시트에 남고 runs row 는 `running` 으로 영구히 남음 → 추후 분석 시 "abnormal termination" 으로 식별 |
| Streamlit rerun 으로 collector 재생성 | 모듈 global active collector 가 None 이 아니면 stop 후 교체 |
| 백그라운드 thread 잔존 | daemon=True + Event stop, join timeout 0.5s |
| `cpu_percent` 첫 샘플 0 문제 | sampler 시작 시 `process.cpu_percent(interval=None)` 1회 워밍업 호출 후 buffer 에 안 넣음 |
| `runs` row update 시 row 번호 캐시 무효화 | 1차에는 매 update 시 `find(run_id)` 로 lookup. 성능 문제 시 캐시 |
| `samples` 시트 row 폭증 | 일단 단일 시트, 분기 시 월별 탭으로 이관 (후속) |
| Streamlit Cloud 에 `.git` 없음 | env `APP_VERSION` 우선, subprocess 실패 시 `"unknown"` |
| flusher 가 Sheets 호출 중 hang | sampler 는 영향 없음 (lock 분리). `stop_and_finalize` 의 join timeout 1.0s 로 차단. finalize 는 `_sink_io_lock` 획득 후에만 sink 재호출 (§4 참고) |
| sampler/flusher 내부 예외 | thread 함수 최외곽 while 안에서 `try/except: logging.exception(...)` 로 감싸 thread 사망 방지 |
| Sheets hang + 장시간 미flush | sampler buffer 가 `MAX_BUFFER_ROWS = 2000` 초과 시 head drop. `n_dropped_samples` 증가. 메모리는 항상 bounded |
| `running` append 자체 실패 | collector `_running_row = None` 보관. finalize 시 append-only 경로로 종료 row 1행 새로 push. `was_append_only=true` 로 구분 가능 |
| `find(run_id)` 가 header 와 충돌 | `find` 호출을 `run_id` 컬럼 범위 (예: `range="A:A"`) 로 한정 |
| `n_failed_chunks` 중복 카운트 | translation.py 에서는 incr 절대 금지. runner 의 `future.result()` try/except 한 곳에서만 (`record_failed_chunk()`) |
| recursive split fallback 에 metrics 누락 | `translate_text_with_gemini` 재귀 호출에 `metrics=metrics` 명시 — PR 리뷰 체크 항목 |
| `str(e)` 자체가 예외 발생 | `error_short` 는 `try: str(e) except: repr(e)` 로 방어 후 절단 |

## 8. 검증 / Rollout

1. **로컬 단위 테스트** (`tests/test_metrics.py` 신규)
   - `MetricsCollector.incr` thread-safe 확인 (여러 thread 에서 동시 호출 → 합산 일치)
   - `NullMetricsCollector` 가 모든 호출 무시 확인 (인터페이스 전체 시그니처 일치)
   - Sheets sink 를 Mock 으로 주입해 batch flush 호출 횟수/페이로드 확인
   - **Mock sink 가 예외를 던지는 경우** 번역 결과/리턴 값이 영향받지 않는지 확인
   - **flusher 내부에서 예외** 가 났을 때 sampler thread 가 살아 있는지 확인
   - phase duration 합계 ≈ total duration (오차 < 0.5s) 확인
2. **로컬 E2E**
   - 짧은 docx(3~5 chunk) 로 stdout sink 로 동작 확인
   - psutil 값 정상 범위 (RAM > 0, threads >= 1)
3. **Sheets 연결 검증**
   - dummy run_id 로 runs append + samples batch append → 시트에서 눈으로 확인
   - update 도 동작하는지 확인 (run_id find → ended_at 채워짐)
4. **Streamlit Cloud 배포 검증**
   - secrets 등록, `METRICS_ENABLED=True` → 실제 문서 1건 번역 → 시트에 row 생성 확인
   - 일부러 GEMINI_API_KEY 를 잘못 넣어 예외 발생시키고 status=error 기록되는지 확인
   - **finalize 미호출 시나리오**: 번역 도중 브라우저 탭 강제 종료 → runs row 가 `running` 으로 stale 상태 유지되는지 + samples 에 직전까지의 row 가 들어와 있는지 확인
   - **Streamlit rerun 시 collector 교체**: 번역 중 파일 재업로드/다른 위젯 조작으로 rerun 유발 후 active collector 교체가 깔끔히 되는지 확인
   - **Sheets API 실패 강제** (서비스 어카운트 권한 잠시 회수) → 번역 결과 자체는 정상 다운로드 되는지 확인 후 권한 복구
5. **수동 차트 셋업** (1차 후)
   - `runs`: workers vs peak_ram_mb, workers vs duration_translate_s scatter
   - `samples`: run_id 필터 + t_offset_s vs ram_mb line
6. **A/B 실험**
   - 동일 문서로 workers ∈ {8, 12, 16} 각 3회 → `runs` 시트에서 비교
7. **롤백**
   - `METRICS_ENABLED=False` secret 변경으로 즉시 비활성, 재배포 불필요

## 9. 오픈 이슈 / 결정 필요

1. `APP_VERSION` 주입을 Streamlit Cloud secret 으로 갱신할지, git fallback 만으로 갈지. **1차는 env → "unknown" 만으로도 충분 (codex 2차 의견)**, git SHA fallback 은 1차에서 빼도 됨.
2. `runs` row update 가 일관되게 실패할 경우 별도 `runs_events` 시트에 append-only 로 fallback 할지 (v4 의 `was_append_only` 컬럼 + append-only 경로로 일부 대응됨 → 1차에서는 별도 시트 미신설).
3. samples flush 주기 30s / batch 30 row 가 적절한지 — 1차 배포 후 시트에서 직접 확인.
4. Discord webhook 후속 추가 시점 (1차 안정화 후 별도 PR).
5. **1차 컬럼 축소 옵션 (codex 2차 제안)**: `avg_process_cpu_pct`, `peak_process_threads`, `duration_build_doc_s` 는 핵심 목표(peak RAM, retry, workers tuning)에 직접 기여 미약. 1차 그대로 유지 (NULL 허용) vs 1차 제거 — 사용자 결정 필요. 현재 플랜은 **유지** 쪽.

> v2 까지 open 이었던 `n_failed_chunks` 위치는 v3 에서 **runner 의 `future.result()` 단** 으로 확정.
> v3 의 lock 모호점은 v4 에서 lock 획득 순서 (`counter → buffer → sink_io`) 와 `_SheetsSink` IO lock + finalize ordering 으로 확정.
> v3 의 bounded buffer 모호점은 v4 에서 `MAX_BUFFER_ROWS = 2000` + drop-oldest + `n_dropped_samples` 카운팅으로 확정.

## 9-1. 리뷰 사이클 결론

- codex 1차 리뷰 → v2/v3 반영.
- codex 2차 리뷰 결론: **conditional go** (`MAX_BUFFER_ROWS` 폐기 정책, `_SheetsSink` IO lock + finalize 동시성 명시 조건).
- v4 에서 두 조건 명시 완료 → **go**.
- codex 3차 sanity check: 두 조건 해소 확인. implementation-time 권고 1건(`sink_io_lock` 대기 timeout) → v4.1 에서 finalize 의 `acquire(timeout=3.0s)` + skip + warning 으로 반영. **최종 go.**

## 10. 작업 추정 (1차 PR 한정)

| 단계 | 예상 |
|---|---|
| `MetricsCollector` + `NullMetricsCollector` + lock 카운터 + samples buffer | 1.5h |
| `_SheetsSink` (gspread auth + append + find/update) | 1.5h |
| sampler / flusher thread 분리 + cpu_percent 워밍업 + lock 순서 검증 | 1.5h |
| `translation.py` + `translation_runner.py` 카운터 hook (api_call / 429 / mismatch / split / failed_chunk) | 1.5h |
| `app.py` 통합 + run_meta(record) + try/except/finally + phase 전환 | 1h |
| 단위 테스트 (mock sink 예외, flusher 사망 방지, phase duration 합 검증 포함) | 1.5h |
| 로컬 검증 | 1h |
| Streamlit Cloud 배포 + secrets + rerun/finalize 미호출/Sheets 실패 강제 검증 | 2.5h |
| **합계** | **~12h** (codex v2 review 권고 10~14h 중간값) |
