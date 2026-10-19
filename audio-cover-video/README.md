# Audio Cover Video

이미지 한 장과 오디오 파일(mp4, m4a, wav, mp3 등)을 올리면, 오디오 길이만큼 이미지를 커버로 깐 mp4 동영상을 만들어 주는 NAS용 웹서비스입니다. 아이폰 Safari에서 결과를 **사진 앱에 바로 저장**할 수 있습니다.

- 백엔드: Python / FastAPI + ffmpeg (Docker 컨테이너 1개)
- 프론트: 모바일 우선 단일 페이지 (로그인·편집 기능 없음)
- 출력: **1920×1080, 60fps** 고정. H.264 High@4.2 + AAC, yuv420p, `+faststart` (아이폰 사진 앱 호환)
- 16:9가 아닌 이미지는 비율을 유지해 가운데 배치하고, 빈 영역은 같은 이미지를 흐리고 어둡게 처리한 배경으로 채웁니다

## Synology Container Manager 배포

1. 이 폴더(`audio-cover-video`)를 NAS의 `docker` 공유 폴더에 복사합니다. 예: `/volume1/docker/audio-cover-video`
2. DSM → **Container Manager** → **프로젝트** → **생성**
   - 프로젝트 이름: `audio-cover-video`
   - 경로: 위에서 복사한 폴더 선택
   - 소스: **docker-compose.yml 사용** (폴더 안의 파일이 자동으로 잡힙니다)
3. **다음** → **완료**. 처음 한 번은 이미지를 빌드하므로 몇 분 걸립니다.
4. 같은 Wi‑Fi의 아이폰에서 `http://NAS-IP:8080` 으로 접속합니다.

포트를 바꾸려면 `docker-compose.yml`의 `"8080:8080"` 앞쪽 숫자만 수정하세요.

### HTTPS 설정 (권장)

"사진 앱에 저장" 버튼은 브라우저 Web Share API를 쓰는데, 이 기능은 **HTTPS에서만** 동작합니다. HTTP로 접속하면 버튼 대신 다운로드 안내가 표시됩니다 (아래 참고).

DSM 리버스 프록시로 HTTPS를 붙이는 방법:

1. **제어판 → 로그인 포털 → 고급 → 리버스 프록시 → 생성**
   - 소스: 프로토콜 `HTTPS`, 호스트 이름 `*` 또는 NAS 도메인(예: `xxx.synology.me`), 포트 `8443`
   - 대상: 프로토콜 `HTTP`, 호스트 이름 `localhost`, 포트 `8080`
2. **제어판 → 보안 → 인증서**에서 Let's Encrypt 인증서를 발급하고 위 리버스 프록시에 연결합니다. (QuickConnect/DDNS로 `xxx.synology.me` 도메인을 쓰면 무료로 발급됩니다.)
3. 아이폰에서 `https://xxx.synology.me:8443` 으로 접속합니다.

리버스 프록시에서 업로드 용량 제한이 걸리면 해당 프록시 항목의 **사용자 지정 헤더 / 고급 설정**에서 proxy body size를 늘리세요.

## 아이폰에서 저장하기

| 접속 방식 | 저장 방법 |
|---|---|
| HTTPS | 완료 화면의 **사진 앱에 저장** → 공유 시트에서 **비디오 저장** |
| HTTP | **다운로드** → 파일 앱에서 mp4 열기 → 공유 → **비디오 저장**. 또는 "새 탭에서 열기" → Safari 공유 버튼 → **비디오 저장** |

아이폰 사진 앱에서 HEIC 사진을 고르면 Safari가 자동으로 JPEG로 변환해 올립니다. HEIC 파일을 직접 올려도 서버에서 처리됩니다.

## 설정 (환경 변수)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `MAX_UPLOAD_MB` | 1024 | 파일 하나당 업로드 상한 |
| `JOB_TTL_HOURS` | 6 | 결과 mp4 보관 시간. 지나면 자동 삭제 |
| `MAX_CONCURRENT_JOBS` | 1 | 동시에 처리할 변환 수. NAS CPU에 맞춰 조절 |
| `DATA_DIR` | /data | 작업 파일 저장 경로 (컨테이너 내부) |

## API

| 메서드 | 경로 | 설명 |
|---|---|---|
| `POST` | `/api/jobs` | multipart `image`, `audio` 업로드. `202` + 작업 정보 |
| `GET` | `/api/jobs/{id}` | 상태 조회 (`queued` / `processing` / `done` / `error`, `progress` 0~1) |
| `GET` | `/api/jobs/{id}/download` | 결과 mp4 (`?inline=1` 이면 브라우저에서 바로 재생) |
| `GET` | `/api/health` | 헬스체크 |

```bash
curl -F image=@cover.jpg -F audio=@track.m4a http://NAS-IP:8080/api/jobs
# {"id":"a1b2c3d4e5f6","status":"queued",...}
curl -o out.mp4 http://NAS-IP:8080/api/jobs/a1b2c3d4e5f6/download
```

## 로컬 개발

```bash
cd audio-cover-video
pip install -r requirements.txt pytest httpx    # ffmpeg 가 PATH 에 있어야 함
DATA_DIR=./data uvicorn app.main:app --reload --port 8080
python -m pytest tests
```

## 변환 방식

1. 이미지: EXIF 회전 반영 → RGB 변환 → 1920×1080 프레임 합성 (Pillow). 비율 유지로 가운데 배치, 빈 영역은 같은 이미지를 꽉 채우게 확대 후 가우시안 블러 + 60% 밝기로 채움
2. 오디오: `ffprobe`로 길이와 코덱 확인. 이미 AAC면 그대로 복사, 아니면 AAC 192k로 변환. mp4/mov를 올리면 오디오 트랙만 사용
3. 5초짜리 정지 화면 세그먼트 하나만 `libx264 -tune stillimage` 로 60fps 인코딩한 뒤, concat demuxer로 스트림 복사(재인코딩 없음)하여 오디오 길이(`-t`)만큼 이어 붙이고 오디오를 얹습니다. 비디오 인코딩이 오디오 길이와 무관하게 한 번뿐이라 1080p60이어도 NAS에서 수 초~수십 초면 끝납니다. 키프레임은 5초마다 하나입니다
