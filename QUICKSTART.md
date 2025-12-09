# 빠른 시작 가이드

## 5분 안에 시작하기

### 1단계: 설치 (30초)

```bash
# 프로젝트 디렉토리로 이동
cd MysqlTestDataProvisioner

# 의존성 설치
pip install -r requirements.txt
```

### 2단계: DB 연결 설정 (1분)

프로파일의 연결 정보 수정 (예: `config/wpm/connection.json`):

```json
{
  "host": "localhost",
  "port": 3306,
  "user": "your_username",
  "password": "your_password",
  "database": "your_database"
}
```

### 3단계: 프로그램 실행 (3분)

```bash
python3 mysql-test-data-provisioner.py
```

### 4단계: 데이터 생성

TUI 화면에서:

```
1 → 프로파일 선택 (예: wpm 또는 example)
2 → 시나리오 선택 (예: small-test.json)
3 → 분석 실행
4 → 스키마 생성 (처음 사용 시)
5 → 데이터 생성
```

완료! 🎉

---

## 주요 명령어

| 키 | 기능 |
|---|---|
| 1 | 프로파일 선택 |
| 2 | 시나리오 선택 |
| 3 | 스키마/시나리오 분석 |
| 4 | 스키마 생성 (테이블) |
| 5 | 테스트 데이터 생성 |
| 6 | 데이터 롤백 |
| 7 | MySQL 덤프 |
| Q | 종료 |

---

## 예제 프로파일

프로젝트에는 두 가지 예제가 포함되어 있습니다:

### 1. WPM 프로파일
- 위치: `config/wpm/`, `scenario/wpm/`
- 간단한 사용자/주문 테이블

### 2. Example 프로파일
- 위치: `config/example/`, `scenario/example/`
- 고객/제품/주문/리뷰 시스템
- 두 가지 시나리오:
  - `small-test.json`: 소량 데이터 (개발용)
  - `large-test.json`: 대량 데이터 (성능 테스트용)

---

## 다음 단계

✅ 기본 사용법 익히기 → [USAGE.md](USAGE.md) 참고  
✅ 새 프로파일 만들기 → [README.md](README.md) 참고  
✅ 커스텀 시나리오 작성 → [README.md](README.md) 참고  

---

## 문제 해결

**프로그램이 "이미 실행 중"이라고 표시될 때:**
```bash
rm .mysql_testdata_generator.lock
```

**DB 연결 실패:**
- `config/{profile}/connection.json` 확인
- MySQL 서버 실행 상태 확인
- 계정 권한 확인

**자세한 도움말:**
- [USAGE.md](USAGE.md) - 상세 사용 가이드
- [README.md](README.md) - 전체 문서

