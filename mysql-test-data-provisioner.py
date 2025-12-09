#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MySQL 자동 테스트 데이터 생성 프로그램 (TUI)

기능 개요
1. MySQL 스키마 파일 읽기 및 파싱 (간단한 CREATE TABLE 분석)
2. 시나리오 파일(JSON) 읽기 → 연관관계/레코드 수 분석
3. 스키마/시나리오에 맞춰 테스트 데이터 생성 후 DB에 INSERT
4. 생성된 데이터 롤백 기능 (로컬 run 로그 파일 기반 DELETE)
5. mysqldump 호출로 덤프 기능 제공
6. 스키마(DDL)는 절대 수정 금지, 레코드(INSERT/DELETE)만 조작
7. 락 파일을 이용한 프로그램 중복 실행 방지
8. curses 기반 TUI:
   - 상단: 메뉴 표시
   - 중간: 상태/로그 영역
   - 하단: 사용자 입력 / 진행률 / 커맨드 표시
"""

import os
import sys
import json
import time
import random
import string
import subprocess
import signal
import glob
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Tuple

# MySQL 연결 라이브러리
try:
    import mysql.connector
except ImportError:
    print("mysql-connector-python 라이브러리를 설치하세요: pip install mysql-connector-python")
    sys.exit(1)

# TUI용 curses
try:
    import curses
except ImportError:
    print("이 프로그램은 curses 모듈이 필요합니다. (Windows의 경우 'pip install windows-curses')")
    sys.exit(1)


# ==========================
# 설정 & 상수
# ==========================

LOCK_FILE = ".mysql_testdata_generator.lock"
WORK_DIR = "work"  # 각 실행(run)마다 삽입된 PK 정보를 JSON으로 저장
DUMP_DIR = "dump"  # MySQL 덤프 파일 저장
CONFIG_DIR = "config"
SCENARIO_DIR = "scenario"


# ==========================
# 유틸리티
# ==========================

def acquire_lock():
    """
    간단한 락 파일 기반 중복 실행 방지.
    이미 락 파일이 있으면 프로그램 종료.
    """
    if os.path.exists(LOCK_FILE):
        print(f"이미 다른 인스턴스가 실행 중입니다. (lock file: {LOCK_FILE})")
        sys.exit(1)
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))


def release_lock():
    """종료 시 락 파일 제거"""
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass


def random_string(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def random_int(min_val: int = 1, max_val: int = 10000) -> int:
    return random.randint(min_val, max_val)


def ensure_work_dir(profile: str, scenario: str):
    """work/{profile}_{scenario}/ 디렉토리 생성"""
    work_path = os.path.join(WORK_DIR, f"{profile}_{scenario}")
    if not os.path.exists(work_path):
        os.makedirs(work_path, exist_ok=True)
    return work_path


def ensure_dump_dir():
    """dump/ 디렉토리 생성"""
    if not os.path.exists(DUMP_DIR):
        os.makedirs(DUMP_DIR, exist_ok=True)
    return DUMP_DIR


def get_available_profiles() -> List[str]:
    """config 디렉토리에서 사용 가능한 프로파일 목록 반환"""
    if not os.path.exists(CONFIG_DIR):
        return []
    profiles = []
    for item in os.listdir(CONFIG_DIR):
        path = os.path.join(CONFIG_DIR, item)
        if os.path.isdir(path):
            # connection.json과 schema.sql이 있는지 확인
            conn_file = os.path.join(path, "connection.json")
            schema_file = os.path.join(path, "schema.sql")
            if os.path.exists(conn_file) and os.path.exists(schema_file):
                profiles.append(item)
    return sorted(profiles)


def get_available_scenarios(profile: str) -> List[Tuple[str, str]]:
    """특정 프로파일의 시나리오 목록 반환 (파일명, 전체경로)"""
    scenario_path = os.path.join(SCENARIO_DIR, profile)
    if not os.path.exists(scenario_path):
        return []
    
    scenarios = []
    for file in glob.glob(os.path.join(scenario_path, "*.json")):
        filename = os.path.basename(file)
        scenarios.append((filename, file))
    return sorted(scenarios)


def load_connection_config(profile: str) -> Dict[str, Any]:
    """프로파일의 connection.json 로드"""
    conn_file = os.path.join(CONFIG_DIR, profile, "connection.json")
    if not os.path.exists(conn_file):
        raise FileNotFoundError(f"연결 설정 파일을 찾을 수 없습니다: {conn_file}")
    
    with open(conn_file, "r", encoding="utf-8") as f:
        config = json.load(f)
    
    # localhost를 127.0.0.1로 변경 (TCP 연결 강제, Unix socket 문제 방지)
    if config.get("host", "").lower() == "localhost":
        config["host"] = "127.0.0.1"
    
    return config


# ==========================
# 데이터 구조 정의
# ==========================

@dataclass
class ColumnInfo:
    name: str
    type: str
    is_primary: bool = False
    is_nullable: bool = True
    has_default: bool = False


@dataclass
class TableInfo:
    name: str
    columns: List[ColumnInfo] = field(default_factory=list)
    primary_key: Optional[str] = None


@dataclass
class SchemaInfo:
    tables: Dict[str, TableInfo] = field(default_factory=dict)


@dataclass
class ScenarioTable:
    name: str
    count: int
    relations: Dict[str, str] = field(default_factory=dict)  # column_name -> "parent_table.parent_pk"


@dataclass
class ScenarioInfo:
    tables: Dict[str, ScenarioTable] = field(default_factory=dict)


@dataclass
class RunLogEntry:
    run_id: str
    created_at: str
    profile: str
    scenario: str
    inserted_rows: Dict[str, List[int]]  # table_name -> [pk, pk, ...]


# ==========================
# 스키마 파서
# ==========================

class MySQLSchemaParser:
    """
    매우 단순한 CREATE TABLE 파서.
    - 기본적인 테이블 이름, 컬럼 이름/타입, PRIMARY KEY만 파싱.
    - 복잡한 DDL(인덱스, 외래키, 제약조건 등)은 최소한만 처리.
    """

    def __init__(self, schema_file: str):
        self.schema_file = schema_file

    def parse(self) -> SchemaInfo:
        if not os.path.exists(self.schema_file):
            raise FileNotFoundError(f"스키마 파일을 찾을 수 없습니다: {self.schema_file}")

        with open(self.schema_file, "r", encoding="utf-8") as f:
            sql = f.read()

        # CREATE TABLE 블록 단위로 분리
        blocks = self._split_create_table_blocks(sql)
        schema = SchemaInfo()

        for block in blocks:
            table_info = self._parse_table_block(block)
            if table_info is not None:
                schema.tables[table_info.name] = table_info

        return schema
    
    def get_create_statements(self) -> List[str]:
        """스키마 파일에서 CREATE TABLE 문들을 추출"""
        if not os.path.exists(self.schema_file):
            raise FileNotFoundError(f"스키마 파일을 찾을 수 없습니다: {self.schema_file}")

        with open(self.schema_file, "r", encoding="utf-8") as f:
            sql = f.read()

        return self._split_create_table_blocks(sql)

    def _split_create_table_blocks(self, sql: str) -> List[str]:
        blocks = []
        current = []
        in_create = False

        for line in sql.splitlines():
            stripped = line.strip()
            if stripped.upper().startswith("CREATE TABLE"):
                if current:
                    blocks.append("\n".join(current))
                    current = []
                in_create = True
            if in_create:
                current.append(line)
                if stripped.endswith(";"):
                    blocks.append("\n".join(current))
                    current = []
                    in_create = False

        if current:
            blocks.append("\n".join(current))

        return blocks

    def _parse_table_block(self, block: str) -> Optional[TableInfo]:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            return None

        # CREATE TABLE `table_name` ( ... );
        first = lines[0]
        name = None
        if "`" in first:
            # CREATE TABLE `table_name` (
            try:
                name = first.split("`", 2)[1]
            except Exception:
                pass
        else:
            # CREATE TABLE table_name (
            parts = first.split()
            if len(parts) >= 3:
                name = parts[2].strip(" (")

        if not name:
            return None

        table = TableInfo(name=name)

        # 컬럼 및 PRIMARY KEY 파싱
        for line in lines[1:]:
            if line.startswith(")"):
                break

            # PRIMARY KEY
            if line.upper().startswith("PRIMARY KEY"):
                # PRIMARY KEY (`id`)
                if "`" in line:
                    pk_name = line.split("`", 2)[1]
                    table.primary_key = pk_name
                    for c in table.columns:
                        if c.name == pk_name:
                            c.is_primary = True
                continue

            # 컬럼 정의: `col` TYPE ...
            if line.startswith("`"):
                try:
                    parts = line.split()
                    col_name = parts[0].strip("`,")
                    col_name = col_name.strip("`")
                    col_type = parts[1]
                    is_nullable = "NOT NULL" not in line.upper()
                    has_default = "DEFAULT" in line.upper()
                    
                    # 인라인 PRIMARY KEY 체크 (예: `id` INT AUTO_INCREMENT PRIMARY KEY)
                    is_primary = "PRIMARY KEY" in line.upper()
                    
                    col = ColumnInfo(
                        name=col_name,
                        type=col_type,
                        is_primary=is_primary,
                        is_nullable=is_nullable,
                        has_default=has_default,
                    )
                    table.columns.append(col)
                    
                    # 인라인 PRIMARY KEY인 경우 테이블의 primary_key 설정
                    if is_primary and not table.primary_key:
                        table.primary_key = col_name
                        
                except Exception:
                    # 파싱 실패 시 무시
                    continue

        return table


# ==========================
# 시나리오 로더
# ==========================

class ScenarioLoader:
    """
    scenario.json 포맷:
    {
      "tables": {
        "users": { "count": 10 },
        "orders": {
          "count": 30,
          "relations": {
            "user_id": "users.id"
          }
        }
      }
    }
    """

    def __init__(self, scenario_file: str):
        self.scenario_file = scenario_file

    def load(self) -> ScenarioInfo:
        if not os.path.exists(self.scenario_file):
            raise FileNotFoundError(f"시나리오 파일을 찾을 수 없습니다: {self.scenario_file}")

        with open(self.scenario_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        tables = {}
        for tname, tinfo in data.get("tables", {}).items():
            count = int(tinfo.get("count", 0))
            relations = tinfo.get("relations", {}) or {}
            tables[tname] = ScenarioTable(
                name=tname,
                count=count,
                relations=relations,
            )

        return ScenarioInfo(tables=tables)


# ==========================
# 스키마 생성 매니저
# ==========================

class SchemaCreator:
    """
    스키마 파일을 읽어서 실제 DB에 테이블 생성
    - 기존 테이블은 건드리지 않음
    - 없는 테이블만 생성
    """
    
    def __init__(self, schema_file: str, db_config: Dict[str, Any]):
        self.schema_file = schema_file
        self.db_config = db_config
        self.conn = None
    
    def connect(self):
        self.conn = mysql.connector.connect(**self.db_config)
    
    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None
    
    def get_existing_tables(self) -> List[str]:
        """DB에 존재하는 테이블 목록 조회"""
        if not self.conn:
            self.connect()
        
        cursor = self.conn.cursor()
        cursor.execute("SHOW TABLES")
        tables = [row[0] for row in cursor.fetchall()]
        cursor.close()
        return tables
    
    def create_missing_tables(self, progress_callback=None) -> Dict[str, str]:
        """
        스키마 파일의 테이블 중 DB에 없는 것만 생성
        반환: {"table_name": "success|error_message", ...}
        """
        if not self.conn:
            self.connect()
        
        # 기존 테이블 조회
        existing_tables = self.get_existing_tables()
        
        # CREATE TABLE 문 추출
        parser = MySQLSchemaParser(self.schema_file)
        create_statements = parser.get_create_statements()
        
        results = {}
        cursor = self.conn.cursor()
        
        for idx, statement in enumerate(create_statements):
            # 테이블 이름 추출
            table_name = self._extract_table_name(statement)
            if not table_name:
                continue
            
            if progress_callback:
                progress_callback(table_name, idx + 1, len(create_statements))
            
            # 이미 존재하면 스킵
            if table_name in existing_tables:
                results[table_name] = "already_exists"
                continue
            
            # 테이블 생성
            try:
                cursor.execute(statement)
                self.conn.commit()
                results[table_name] = "created"
            except Exception as e:
                results[table_name] = f"error: {str(e)}"
                self.conn.rollback()
        
        cursor.close()
        return results
    
    def _extract_table_name(self, create_statement: str) -> Optional[str]:
        """CREATE TABLE 문에서 테이블 이름 추출"""
        lines = create_statement.strip().split('\n')
        if not lines:
            return None
        
        first_line = lines[0].strip()
        
        # CREATE TABLE `table_name`
        if "`" in first_line:
            try:
                return first_line.split("`", 2)[1]
            except Exception:
                return None
        else:
            # CREATE TABLE table_name
            parts = first_line.split()
            if len(parts) >= 3:
                return parts[2].strip(" (;")
        
        return None


# ==========================
# MySQL 연결 & 데이터 생성
# ==========================

class TestDataGenerator:
    """
    - 스키마/시나리오 기반으로 랜덤 데이터를 생성하여 INSERT
    - 각 실행(run_id)마다 삽입된 PK를 기록하여 롤백에 활용
    """

    def __init__(self, schema: SchemaInfo, scenario: ScenarioInfo, db_config: Dict[str, Any], profile: str, scenario_name: str):
        self.schema = schema
        self.scenario = scenario
        self.db_config = db_config
        self.profile = profile
        self.scenario_name = scenario_name
        self.conn = None

    def connect(self):
        self.conn = mysql.connector.connect(**self.db_config)
        self.conn.autocommit = False  # 트랜잭션 사용

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def generate_and_insert(self, progress_callback=None) -> RunLogEntry:
        """
        - 시나리오에 정의된 테이블 순서대로 INSERT
        - relations가 있는 경우, 부모 테이블에서 랜덤 PK를 골라 FK 셋팅
        - run_log에 각 테이블별 생성된 PK를 기록
        - progress_callback: 진행률 표시 콜백 함수
        """
        if not self.conn:
            self.connect()

        cursor = self.conn.cursor()
        run_id = datetime.now().strftime("%Y%m%d%H%M%S")
        inserted_rows: Dict[str, List[int]] = {}

        try:
            # 전체 카운트 계산
            total_count = sum(stable.count for stable in self.scenario.tables.values())
            current_count = 0
            
            # 부모 → 자식 순서를 단순히 scenario의 정의 순서대로 처리
            for tname, stable in self.scenario.tables.items():
                if stable.count <= 0:
                    continue
                if tname not in self.schema.tables:
                    # 시나리오에 있지만 스키마에 없는 테이블
                    continue

                table_info = self.schema.tables[tname]
                if not table_info.primary_key:
                    # PK 없는 테이블은 롤백이 어려우므로 건너뜀
                    continue

                inserted_rows[tname] = []

                for i in range(stable.count):
                    col_names, values = self._build_row_values(table_info, stable, inserted_rows)
                    placeholders = ", ".join(["%s"] * len(values))
                    cols_part = ", ".join(f"`{c}`" for c in col_names)

                    sql = f"INSERT INTO `{tname}` ({cols_part}) VALUES ({placeholders})"
                    cursor.execute(sql, values)
                    pk_val = cursor.lastrowid
                    inserted_rows[tname].append(pk_val)
                    
                    current_count += 1
                    if progress_callback and total_count > 0:
                        progress_callback(tname, current_count, total_count)

            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            raise e
        finally:
            cursor.close()

        run_log = RunLogEntry(
            run_id=run_id,
            created_at=datetime.now().isoformat(),
            profile=self.profile,
            scenario=self.scenario_name,
            inserted_rows=inserted_rows,
        )
        self._save_run_log(run_log)
        return run_log

    def _build_row_values(
        self,
        table_info: TableInfo,
        scenario_table: ScenarioTable,
        inserted_rows: Dict[str, List[int]],
    ) -> Tuple[List[str], List[Any]]:
        """
        단순한 랜덤 값 생성 + relations 반영
        - VARCHAR류: 랜덤 문자열
        - INT류: 랜덤 정수
        - relations에 정의된 FK는 부모 테이블에서 랜덤 PK 선택
        """
        col_names = []
        values = []

        for col in table_info.columns:
            # AUTO_INCREMENT PK는 INSERT 시 생략 (MySQL이 자동 생성)
            if col.is_primary:
                continue

            # relations 처리 (FK)
            if col.name in scenario_table.relations:
                rel = scenario_table.relations[col.name]  # "parent_table.parent_pk"
                parent_table, parent_pk = rel.split(".")
                parent_ids = inserted_rows.get(parent_table, [])
                if not parent_ids:
                    # 부모 데이터가 아직 없으면 NULL (또는 나중에 보완 가능)
                    val = None
                else:
                    val = random.choice(parent_ids)
            else:
                # 타입에 따른 간단 값 생성
                upper_type = col.type.upper()
                if "INT" in upper_type:
                    val = random_int()
                elif "CHAR" in upper_type or "TEXT" in upper_type:
                    val = random_string(12)
                elif "DATE" in upper_type or "TIME" in upper_type:
                    # 아주 단순히 현재 날짜/시간 문자열
                    if "DATETIME" in upper_type or "TIMESTAMP" in upper_type:
                        val = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        val = datetime.now().strftime("%Y-%m-%d")
                else:
                    # 기타 타입은 문자열로 대체
                    val = random_string(8)

            col_names.append(col.name)
            values.append(val)

        return col_names, values

    def _save_run_log(self, run_log: RunLogEntry):
        # 시나리오 파일명에서 확장자 제거
        scenario_name = os.path.splitext(run_log.scenario)[0]
        work_path = ensure_work_dir(run_log.profile, scenario_name)
        path = os.path.join(work_path, f"run_{run_log.run_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "run_id": run_log.run_id,
                    "created_at": run_log.created_at,
                    "profile": run_log.profile,
                    "scenario": run_log.scenario,
                    "inserted_rows": run_log.inserted_rows,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )


# ==========================
# 롤백 매니저
# ==========================

class RollbackManager:
    """
    run_YYYYMMDDHHMMSS.json 파일을 기반으로 INSERT 했던 레코드를 DELETE.
    - 스키마 변경 없이 PK 기반으로만 삭제
    """

    def __init__(self, schema: SchemaInfo, db_config: Dict[str, Any]):
        self.schema = schema
        self.db_config = db_config

    def list_runs(self) -> List[Dict[str, str]]:
        """
        실행 로그 목록 반환
        반환값: [{"run_id": "...", "created_at": "...", "profile": "...", "scenario": "...", "file_path": "..."}, ...]
        """
        if not os.path.exists(WORK_DIR):
            return []
        
        runs = []
        # work 디렉토리 내 모든 프로파일_시나리오 폴더 스캔
        for profile_scenario_dir in os.listdir(WORK_DIR):
            dir_path = os.path.join(WORK_DIR, profile_scenario_dir)
            if not os.path.isdir(dir_path):
                continue
            
            # 각 폴더 내의 run_*.json 파일 스캔
            for f in os.listdir(dir_path):
                if not (f.startswith("run_") and f.endswith(".json")):
                    continue
                
                file_path = os.path.join(dir_path, f)
                try:
                    with open(file_path, "r", encoding="utf-8") as fp:
                        data = json.load(fp)
                        runs.append({
                            "run_id": data.get("run_id", ""),
                            "created_at": data.get("created_at", ""),
                            "profile": data.get("profile", "N/A"),
                            "scenario": data.get("scenario", "N/A"),
                            "file_path": file_path,  # 전체 경로 저장
                        })
                except Exception:
                    continue
        
        # 최신순 정렬
        runs.sort(key=lambda x: x["run_id"], reverse=True)
        return runs

    def rollback_run(self, run_id: str, file_path: str) -> str:
        if not os.path.exists(file_path):
            return f"run 로그 파일을 찾을 수 없습니다: {file_path}"

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        inserted_rows = data.get("inserted_rows", {})

        conn = mysql.connector.connect(**self.db_config)
        conn.autocommit = False
        cursor = conn.cursor()

        try:
            # 자식 테이블 → 부모 테이블 순서로 삭제하는 것이 안전하지만,
            # 여기서는 단순히 테이블 이름 역순으로 처리 (필요시 개선)
            for tname in reversed(list(inserted_rows.keys())):
                if tname not in self.schema.tables:
                    continue
                table_info = self.schema.tables[tname]
                pk = table_info.primary_key
                if not pk:
                    continue
                ids = inserted_rows[tname]
                if not ids:
                    continue

                # 단순 IN 삭제 (배치 크기 조절 가능)
                chunks = [ids[i:i + 100] for i in range(0, len(ids), 100)]
                for ch in chunks:
                    placeholders = ", ".join(["%s"] * len(ch))
                    sql = f"DELETE FROM `{tname}` WHERE `{pk}` IN ({placeholders})"
                    cursor.execute(sql, ch)

            conn.commit()
        except Exception as e:
            conn.rollback()
            cursor.close()
            conn.close()
            return f"롤백 중 오류 발생: {e}"
        finally:
            cursor.close()
            conn.close()

        return f"run_id={run_id} 롤백 완료"


# ==========================
# 덤프 매니저
# ==========================

class MySQLDumpManager:
    """
    mysqldump 명령을 호출해서 전체 DB 또는 특정 테이블 덤프
    """

    def __init__(self, db_config: Dict[str, Any]):
        self.db_config = db_config

    def dump(self, output_filename: str, tables: Optional[List[str]] = None) -> str:
        # dump 디렉토리 생성
        dump_dir = ensure_dump_dir()
        output_file = os.path.join(dump_dir, output_filename)
        
        # localhost를 127.0.0.1로 변경 (TCP 연결 강제)
        host = self.db_config["host"]
        if host.lower() == "localhost":
            host = "127.0.0.1"
        
        cmd = [
            "mysqldump",
            "-h", host,
            "-P", str(self.db_config["port"]),
            "-u", self.db_config["user"],
            "--protocol=TCP",  # TCP 연결 강제
            self.db_config["database"],
        ]
        if tables:
            cmd.extend(tables)

        # 환경 변수로 비밀번호 전달 (더 안전함)
        env = os.environ.copy()
        env["MYSQL_PWD"] = self.db_config["password"]

        try:
            with open(output_file, "w", encoding="utf-8") as f:
                subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, check=True, env=env)
            return f"mysqldump 완료: {output_file}"
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr.decode('utf-8') if e.stderr else str(e)
            return f"mysqldump 실패: {error_msg}"
        except Exception as e:
            return f"mysqldump 오류: {e}"


# ==========================
# TUI 애플리케이션
# ==========================

MENU_ITEMS = [
    "1: 프로파일 선택",
    "2: 시나리오 선택", 
    "3: 스키마/시나리오 분석",
    "4: 스키마 생성 (테이블)",
    "5: 테스트 데이터 생성",
    "6: 데이터 롤백",
    "7: MySQL 덤프",
    "Q: 종료",
]


@dataclass
class AppState:
    current_profile: Optional[str] = None
    current_scenario: Optional[str] = None
    schema: Optional[SchemaInfo] = None
    scenario: Optional[ScenarioInfo] = None
    db_config: Optional[Dict[str, Any]] = None
    last_message: str = ""
    progress: str = ""
    current_menu: int = 1
    log_messages: List[str] = field(default_factory=list)
    
    def add_log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_messages.append(f"[{timestamp}] {message}")
        if len(self.log_messages) > 100:
            self.log_messages = self.log_messages[-100:]


class TUIApplication:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.state = AppState()
        
        # 색상 초기화
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLUE)  # 헤더
        curses.init_pair(2, curses.COLOR_GREEN, -1)  # 성공
        curses.init_pair(3, curses.COLOR_RED, -1)  # 에러
        curses.init_pair(4, curses.COLOR_YELLOW, -1)  # 경고
        curses.init_pair(5, curses.COLOR_CYAN, -1)  # 정보

    def run(self):
        curses.curs_set(0)  # 커서 숨김
        self.stdscr.nodelay(False)
        self.stdscr.clear()
        self.state.add_log("프로그램 시작")

        while True:
            self.draw()
            c = self.stdscr.getch()

            if c == ord('1'):
                self.handle_select_profile()
            elif c == ord('2'):
                self.handle_select_scenario()
            elif c == ord('3'):
                self.handle_analyze()
            elif c == ord('4'):
                self.handle_create_schema()
            elif c == ord('5'):
                self.handle_generate()
            elif c == ord('6'):
                self.handle_rollback()
            elif c == ord('7'):
                self.handle_dump()
            elif c in (ord('q'), ord('Q')):
                break

    # --------------------------
    # UI 그리기
    # --------------------------

    def draw(self):
        self.stdscr.clear()
        height, width = self.stdscr.getmaxyx()

        # 화면 분할
        top_height = 4
        bottom_height = 5
        mid_height = height - top_height - bottom_height

        # 상단 헤더
        self.draw_top(0, 0, top_height, width)
        # 중간 컨텐츠
        self.draw_middle(top_height, 0, mid_height, width)
        # 하단 상태바
        self.draw_bottom(top_height + mid_height, 0, bottom_height, width)

        self.stdscr.refresh()

    def draw_top(self, y, x, h, w):
        """상단 헤더 - 타이틀 및 메뉴"""
        # 타이틀
        title = " MySQL 자동 테스트 데이터 생성기 (TUI) "
        try:
            self.stdscr.addstr(y, max(0, (w - len(title)) // 2), title, curses.color_pair(1) | curses.A_BOLD)
        except:
            pass
        
        # 현재 설정 정보
        info_line = f"Profile: {self.state.current_profile or 'None'} | Scenario: {self.state.current_scenario or 'None'}"
        try:
            self.stdscr.addstr(y + 1, 2, info_line, curses.color_pair(5))
        except:
            pass
        
        # 메뉴
        menu_line = " | ".join(MENU_ITEMS)
        try:
            self.stdscr.addstr(y + 2, 2, menu_line)
        except:
            pass

    def draw_middle(self, y, x, h, w):
        """중간 컨텐츠 영역"""
        lines = []
        
        # 현재 상태 정보
        lines.append("═" * (w - 2))
        lines.append("[ 현재 상태 ]")
        lines.append(f"  프로파일: {self.state.current_profile or '선택 안됨 (메뉴 1)'}")
        lines.append(f"  시나리오: {self.state.current_scenario or '선택 안됨 (메뉴 2)'}")
        
        if self.state.schema:
            lines.append(f"  스키마 테이블: {len(self.state.schema.tables)}개")
        if self.state.scenario:
            lines.append(f"  시나리오 테이블: {len(self.state.scenario.tables)}개")
        
        lines.append("")
        lines.append("[ 최근 메시지 ]")
        if self.state.last_message:
            lines.append(f"  {self.state.last_message}")
        
        lines.append("")
        lines.append("[ 로그 ]")
        
        # 로그 메시지 표시 (최신 것부터)
        log_start_line = len(lines)
        remaining_lines = h - log_start_line - 1
        
        if remaining_lines > 0:
            recent_logs = self.state.log_messages[-remaining_lines:]
            for log in recent_logs:
                lines.append(f"  {log}")
        
        # 화면에 출력
        for idx, line in enumerate(lines):
            if idx >= h - 1:
                break
            try:
                display_line = line[:w-2]  # 화면 너비에 맞춤
                self.stdscr.addstr(y + idx, x + 1, display_line)
            except:
                pass

    def draw_bottom(self, y, x, h, w):
        """하단 상태바"""
        try:
            # 구분선
            self.stdscr.addstr(y, 0, "═" * w)
            
            # 진행 상태
            progress = self.state.progress or "대기 중..."
            self.stdscr.addstr(y + 1, 1, f"[진행] {progress}"[:w-2])
            
            # 명령어 안내
            cmd_help = "명령: 1=프로파일 2=시나리오 3=분석 4=스키마생성 5=데이터생성 6=롤백 7=덤프 Q=종료"
            self.stdscr.addstr(y + 2, 1, cmd_help[:w-2], curses.color_pair(4))
        except:
            pass

    # --------------------------
    # 메뉴 핸들러
    # --------------------------
    
    def handle_select_profile(self):
        """프로파일 선택"""
        self.state.current_menu = 1
        profiles = get_available_profiles()
        
        if not profiles:
            self.state.last_message = "사용 가능한 프로파일이 없습니다. config/ 폴더를 확인하세요."
            self.state.add_log("프로파일 없음")
            return
        
        selected_idx = self._show_selection_menu("프로파일 선택", profiles)
        if selected_idx is not None:
            selected_profile = profiles[selected_idx]
            self.state.current_profile = selected_profile
            self.state.last_message = f"프로파일 '{selected_profile}' 선택됨"
            self.state.add_log(f"프로파일 선택: {selected_profile}")
            
            # DB 설정 로드
            try:
                self.state.db_config = load_connection_config(selected_profile)
                self.state.add_log(f"DB 연결 설정 로드 완료")
            except Exception as e:
                self.state.last_message = f"DB 설정 로드 실패: {e}"
                self.state.add_log(f"오류: {e}")
    
    def handle_select_scenario(self):
        """시나리오 선택"""
        self.state.current_menu = 2
        
        if not self.state.current_profile:
            self.state.last_message = "먼저 프로파일을 선택하세요 (메뉴 1)"
            self.state.add_log("프로파일 미선택 경고")
            return
        
        scenarios = get_available_scenarios(self.state.current_profile)
        
        if not scenarios:
            self.state.last_message = f"'{self.state.current_profile}' 프로파일의 시나리오가 없습니다."
            self.state.add_log("시나리오 없음")
            return
        
        # 파일명만 표시
        scenario_names = [name for name, path in scenarios]
        selected_idx = self._show_selection_menu("시나리오 선택", scenario_names)
        
        if selected_idx is not None:
            selected_name, selected_path = scenarios[selected_idx]
            self.state.current_scenario = selected_path
            self.state.last_message = f"시나리오 '{selected_name}' 선택됨"
            self.state.add_log(f"시나리오 선택: {selected_name}")

    def handle_create_schema(self):
        """스키마 생성 (테이블 생성)"""
        self.state.current_menu = 4
        
        if not self.state.current_profile:
            self.state.last_message = "먼저 프로파일을 선택하세요 (메뉴 1)"
            self.state.add_log("프로파일 미선택 경고")
            return
        
        if not self.state.db_config:
            try:
                self.state.db_config = load_connection_config(self.state.current_profile)
            except Exception as e:
                self.state.last_message = f"DB 설정 로드 실패: {e}"
                self.state.add_log(f"오류: {e}")
                return
        
        self.state.progress = "스키마 생성 중..."
        self.state.add_log("스키마 생성 시작")
        self.draw()
        self.stdscr.refresh()
        
        schema_file = os.path.join(CONFIG_DIR, self.state.current_profile, "schema.sql")
        creator = SchemaCreator(schema_file, self.state.db_config)
        
        def progress_callback(table_name, current, total):
            self.state.progress = f"테이블 '{table_name}' 생성 중... ({current}/{total})"
            self.draw()
            self.stdscr.refresh()
        
        try:
            creator.connect()
            self.state.add_log("DB 연결 성공")
            results = creator.create_missing_tables(progress_callback)
            
            # 결과 집계
            created = sum(1 for v in results.values() if v == "created")
            exists = sum(1 for v in results.values() if v == "already_exists")
            errors = sum(1 for v in results.values() if v.startswith("error"))
            
            self.state.last_message = f"스키마 생성 완료: 생성 {created}개, 기존 {exists}개, 오류 {errors}개"
            self.state.add_log(f"생성 완료: {created}개 테이블 생성됨")
            
            # 상세 로그
            for table, result in results.items():
                if result == "created":
                    self.state.add_log(f"  ✓ {table}: 생성됨")
                elif result == "already_exists":
                    self.state.add_log(f"  - {table}: 이미 존재")
                else:
                    self.state.add_log(f"  ✗ {table}: {result}")
                    
        except Exception as e:
            self.state.last_message = f"스키마 생성 실패: {e}"
            self.state.add_log(f"오류: {e}")
        finally:
            creator.close()
            self.state.progress = ""
            self.draw()

    def handle_analyze(self):
        """스키마/시나리오 분석"""
        self.state.current_menu = 3
        
        if not self.state.current_profile:
            self.state.last_message = "먼저 프로파일을 선택하세요 (메뉴 1)"
            self.state.add_log("프로파일 미선택 경고")
            return
        
        if not self.state.current_scenario:
            self.state.last_message = "먼저 시나리오를 선택하세요 (메뉴 2)"
            self.state.add_log("시나리오 미선택 경고")
            return
        
        self.state.progress = "스키마/시나리오 분석 중..."
        self.state.add_log("분석 시작")
        self.draw()
        self.stdscr.refresh()

        try:
            # 스키마 파싱
            schema_file = os.path.join(CONFIG_DIR, self.state.current_profile, "schema.sql")
            parser = MySQLSchemaParser(schema_file)
            schema = parser.parse()

            # 시나리오 로드
            loader = ScenarioLoader(self.state.current_scenario)
            scenario = loader.load()

            self.state.schema = schema
            self.state.scenario = scenario
            self.state.last_message = f"분석 완료: 테이블 {len(schema.tables)}개, 시나리오 테이블 {len(scenario.tables)}개"
            self.state.add_log(f"스키마 분석 완료: {len(schema.tables)}개 테이블")
            self.state.add_log(f"시나리오 로드 완료: {len(scenario.tables)}개 테이블")
        except Exception as e:
            self.state.last_message = f"분석 실패: {e}"
            self.state.add_log(f"오류: {e}")

        self.state.progress = ""
        self.draw()

    def handle_generate(self):
        """테스트 데이터 생성"""
        self.state.current_menu = 5
        
        if not self.state.schema or not self.state.scenario:
            self.state.last_message = "먼저 스키마/시나리오 분석을 수행하세요 (메뉴 3)"
            self.state.add_log("분석 필요 경고")
            self.draw()
            return
        
        if not self.state.db_config:
            self.state.last_message = "DB 연결 설정이 없습니다"
            self.state.add_log("DB 설정 오류")
            self.draw()
            return

        self.state.progress = "테스트 데이터 생성 중..."
        self.state.add_log("데이터 생성 시작")
        self.draw()
        self.stdscr.refresh()

        scenario_name = os.path.basename(self.state.current_scenario) if self.state.current_scenario else "unknown"
        gen = TestDataGenerator(
            self.state.schema, 
            self.state.scenario, 
            self.state.db_config,
            self.state.current_profile,
            scenario_name
        )
        
        def progress_callback(table_name, current, total):
            percent = int((current / total) * 100)
            self.state.progress = f"테이블 '{table_name}' 처리 중... ({current}/{total}, {percent}%)"
            self.draw()
            self.stdscr.refresh()
        
        try:
            gen.connect()
            self.state.add_log("DB 연결 성공")
            run_log = gen.generate_and_insert(progress_callback)
            self.state.last_message = f"데이터 생성 완료! run_id={run_log.run_id}"
            self.state.add_log(f"생성 완료: run_id={run_log.run_id}")
            
            # 생성된 레코드 수 로그
            for table, pks in run_log.inserted_rows.items():
                self.state.add_log(f"  {table}: {len(pks)}개 레코드")
        except Exception as e:
            self.state.last_message = f"데이터 생성 실패: {e}"
            self.state.add_log(f"오류: {e}")
        finally:
            gen.close()
            self.state.progress = ""
            self.draw()

    def handle_rollback(self):
        """데이터 롤백"""
        self.state.current_menu = 6
        self.state.progress = "롤백할 run 목록 조회 중..."
        self.state.add_log("롤백 목록 조회")
        self.draw()
        self.stdscr.refresh()

        # 스키마가 없으면 로드
        if not self.state.schema:
            if not self.state.current_profile:
                self.state.last_message = "프로파일을 선택하거나 스키마를 분석하세요"
                self.state.progress = ""
                self.draw()
                return
            
            try:
                schema_file = os.path.join(CONFIG_DIR, self.state.current_profile, "schema.sql")
                parser = MySQLSchemaParser(schema_file)
                self.state.schema = parser.parse()
            except Exception as e:
                self.state.last_message = f"스키마 파싱 실패: {e}"
                self.state.add_log(f"오류: {e}")
                self.state.progress = ""
                self.draw()
                return

        if not self.state.db_config:
            if self.state.current_profile:
                try:
                    self.state.db_config = load_connection_config(self.state.current_profile)
                except Exception as e:
                    self.state.last_message = f"DB 설정 로드 실패: {e}"
                    self.state.progress = ""
                    self.draw()
                    return
            else:
                self.state.last_message = "DB 연결 설정이 없습니다"
                self.state.progress = ""
                self.draw()
                return

        manager = RollbackManager(self.state.schema, self.state.db_config)
        runs = manager.list_runs()
        self.state.progress = ""

        if not runs:
            self.state.last_message = "롤백 가능한 run 로그가 없습니다"
            self.state.add_log("롤백 가능한 로그 없음")
            self.draw()
            return

        # run 선택
        run_displays = [f"{r['run_id']} [{r['profile']}/{r['scenario']}] {r['created_at']}" for r in runs]
        selected_idx = self._show_selection_menu("롤백할 실행 선택", run_displays)
        
        if selected_idx is None:
            self.state.last_message = "롤백 취소"
            self.state.add_log("롤백 취소됨")
            self.draw()
            return

        selected_run = runs[selected_idx]
        selected_run_id = selected_run['run_id']
        selected_file_path = selected_run['file_path']
        
        self.state.progress = f"run_id={selected_run_id} 롤백 중..."
        self.state.add_log(f"롤백 시작: {selected_run_id}")
        self.draw()
        self.stdscr.refresh()

        msg = manager.rollback_run(selected_run_id, selected_file_path)
        self.state.last_message = msg
        self.state.add_log(msg)
        self.state.progress = ""
        self.draw()

    def handle_dump(self):
        """MySQL 덤프"""
        self.state.current_menu = 7
        
        if not self.state.db_config:
            if self.state.current_profile:
                try:
                    self.state.db_config = load_connection_config(self.state.current_profile)
                except Exception as e:
                    self.state.last_message = f"DB 설정 로드 실패: {e}"
                    return
            else:
                self.state.last_message = "DB 연결 설정이 없습니다. 프로파일을 선택하세요"
                self.state.add_log("DB 설정 없음")
                return
        
        self.state.progress = "mysqldump 실행 중..."
        self.state.add_log("덤프 시작")
        self.draw()
        self.stdscr.refresh()

        dump_mgr = MySQLDumpManager(self.state.db_config)
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        output_file = f"dump_{self.state.db_config['database']}_{ts}.sql"

        msg = dump_mgr.dump(output_file)
        self.state.last_message = msg
        self.state.add_log(msg)
        self.state.progress = ""
        self.draw()
    
    def _show_selection_menu(self, title: str, items: List[str]) -> Optional[int]:
        """
        선택 메뉴 표시
        반환: 선택된 인덱스 (None이면 취소)
        """
        self.stdscr.clear()
        height, width = self.stdscr.getmaxyx()
        
        try:
            self.stdscr.addstr(0, 2, title, curses.A_BOLD)
            self.stdscr.addstr(1, 2, "=" * min(len(title), width - 4))
        except:
            pass
        
        # 항목 표시
        for idx, item in enumerate(items[:height - 5], start=1):
            try:
                display = f"{idx}. {item}"[:width - 4]
                self.stdscr.addstr(idx + 2, 2, display)
            except:
                pass
        
        # 입력 프롬프트
        prompt_y = min(len(items) + 4, height - 2)
        try:
            self.stdscr.addstr(prompt_y, 2, "선택 (0=취소): ")
        except:
            pass
        
        curses.echo()
        curses.curs_set(1)
        
        try:
            choice = self.stdscr.getstr(prompt_y, 18, 10)
            choice_str = choice.decode("utf-8").strip()
        except:
            choice_str = "0"
        finally:
            curses.noecho()
            curses.curs_set(0)
        
        try:
            num = int(choice_str)
        except:
            return None
        
        if num <= 0 or num > len(items):
            return None
        
        return num - 1


# ==========================
# 메인 진입점
# ==========================

def main():
    """메인 함수"""
    # SIGINT 처리 (Ctrl+C)
    def signal_handler(sig, frame):
        release_lock()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # 락 획득
    try:
        acquire_lock()
    except SystemExit:
        return

    # 설정 디렉토리 확인
    if not os.path.exists(CONFIG_DIR):
        print(f"설정 디렉토리가 없습니다: {CONFIG_DIR}")
        print(f"다음 구조로 설정 파일을 생성하세요:")
        print(f"  {CONFIG_DIR}/[profile]/connection.json")
        print(f"  {CONFIG_DIR}/[profile]/schema.sql")
        print(f"  {SCENARIO_DIR}/[profile]/[scenario].json")
        release_lock()
        return
    
    if not os.path.exists(SCENARIO_DIR):
        print(f"시나리오 디렉토리가 없습니다: {SCENARIO_DIR}")
        os.makedirs(SCENARIO_DIR, exist_ok=True)
        print(f"디렉토리를 생성했습니다: {SCENARIO_DIR}")

    try:
        curses.wrapper(lambda stdscr: TUIApplication(stdscr).run())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"프로그램 오류: {e}")
        import traceback
        traceback.print_exc()
    finally:
        release_lock()


if __name__ == "__main__":
    main()