# Agent Factory for Codex

[English](README.md) | 한국어

사용자에게는 사용자가 사용하는 언어 또는 명시적으로 선택한 언어로 응답합니다.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Agent Factory는 사용자가 주도하는 소프트웨어 개발·전달을 위한 Codex 플러그인입니다.
지휘자 하나가 사용자와 대화하며 범위가 정해진 작업을 수행자에게 맡기고, 모든 보고를
요청한 내용과 대조하며, 프로젝트의 문서·계약·교훈을 관리합니다.

## 동작 방식

![사용자는 지휘자와만 대화합니다. 지휘자가 수행요청서를 쓰면 수행자가 작업하고 수행보고서를 씁니다. 지휘자는 보고서를 완료 기준과 대조한 뒤 답합니다. 검증자는 요청할 때만 작업을 확인합니다.](https://raw.githubusercontent.com/KoreanLeeChangHyun/agent-factory-vscode-extension/main/static/images/readme-conductor.png)

- **지휘자(Main).** 대화·계획·인터뷰·배정을 맡습니다. 기본(오케스트레이터 모드)에서는
  변경과 조사를 모두 위임하고, 직접 모드에서는 직접 수행합니다.
- **수행자(Work 프로필).** 열린 변경은 **전문가**(`work`), 이미 정해진 범위의 변경은
  **작업자**(`workLight`), 읽기 전용 조사는 **탐색자**(`explore`), 문서는 **기록자**(`scribe`)가 맡습니다.
- **수행요청서와 수행보고서.** 수행요청서(목표·범위·완료 기준)가 수행자에게 전달되는
  유일한 문서이고, 수행보고서는 결과·변경 경로·수행자의 자체 확인을 돌려줍니다.
- **결과 대조.** 지휘자는 보고 전에 각 보고서를 완료 기준과 대조하고, 어긋나면 같은 작업으로 되돌립니다.
- **요청할 때만 검증.** 선택한 실행 방식이 요구할 때만 별도 Verification 에이전트가 작업을
  확인하며, 지적 사항은 통과할 때까지 Work로 돌아갑니다.
- **도메인.** 모든 수행자는 이름 있는 작업 영역에 속하며, 같은 도메인의 후속 작업은
  그 도메인의 수행자가 받습니다(`domains.py`).

## 핵심 기능

### 1. 문서 시스템

![문서·인터뷰·교훈: 출처가 조사 자료가 되고, 합의된 내용은 명세가 되며, 명세는 에이전트가 불러오는 프로젝트 스킬로 동기화됩니다. 인터뷰는 결정을, 에이전트는 교훈과 계약을 기록하고, 근거가 충분한 교훈은 규칙으로 통합됩니다.](https://raw.githubusercontent.com/KoreanLeeChangHyun/agent-factory-vscode-extension/main/static/images/readme-knowledge.png)

- 원본 출처, 조사·분석, 합의된 명세, 작업 진행 기록, 교훈을 구분하여 관리합니다.
- 문서별 편집 정본을 하나로 유지하고, 근거·가정·사용자가 확정한 결정을 구분합니다.
  문서를 작성하거나 요약한 사실만으로 프로젝트 규칙이 되지는 않습니다.
- 문서 마이그레이션을 요청하면 기존 내용과 참조를 보존하며 프로젝트 구조에 맞게 정리합니다.
- 합의된 명세는 프로젝트 스킬로 동기화되어 이후 작업에서 활용됩니다.

### 2. 계약 → 작업·검증 루프

![계약과 루프: 지휘자가 버전이 있는 계약을 돌려주고, 전문가가 작업·보고하며, 검증자가 완료 기준으로 확인하고 지적 사항은 통과할 때까지 전문가에게 돌아갑니다. 지휘자는 계약 기록과 함께 보고합니다.](https://raw.githubusercontent.com/KoreanLeeChangHyun/agent-factory-vscode-extension/main/static/images/readme-loop.png)

- 대화를 여섯 부분으로 된 작업 계약으로 정리합니다. **계약 정보**, **작업 고용**(에이전트 ID와 역할), **작업 목표**(작업 ID, 담당 에이전트, 완료 기준), **제약 조건**(작업 간 의존 관계 포함), **파일 구조**(작업 ID와 함께 추가·수정·삭제를 표시한 파일 트리), **작업 순서**(제약 조건에서 도출한 작업 단위 다이어그램)입니다.
- 각 계약 버전 `docs/progress/<contract-id>/contract-v<N>.md`에 작업별 실행 기록(실행 ID, 에이전트, Work·Verification 상태, 근거)을 함께 남깁니다. 범위를 바꾸면 새 버전을 만들고 이전 버전은 보존합니다.
- 검증에서 발견한 문제는 Work로 돌아가 수정한 뒤 다시 검증합니다. 해결되지 않은 문제는 결과에 남깁니다.
- 메시지마다 실행 방식을 고릅니다. Main 직접 작업과 Work만 실행하는 방식도 있으며, 별도 검증은 선택한 방식이 요구할 때만 실행합니다.

### 3. 인터뷰

- 기존 대화와 자료로 해결할 수 없는 중요한 요구사항이나 결정을 질문으로 구체화합니다.
- 한 번에 하나의 결정을 다루고, 선택지별 장단점과 권고를 제시합니다. 답변에 따라 다음 질문을 조정합니다.
- 인터뷰를 마치면 결정과 미해결 사항을 요약하고, 요청하면 문서로 보존하여 계약과 계획에 활용합니다.

### 4. 교훈

- 복구된 실패를 포함한 오류와 사용자·에이전트의 판단 차이를 당시 상황, 원인, 시도한 해결책, 결과와 함께 기록합니다.
- 관련 작업 전에 기존 교훈을 조회하고, 적용한 교훈이 도움이 되었는지, 수정이 필요했는지, 다시 실패했는지 기록합니다.
- 런타임은 실행마다 관찰한 사실(도구 실패, 실패한 실행, 재작업, Verification 실패, 사용자의 정정)을 남기고 해당 실행을 교훈 작성 대기 항목에 올립니다.
  Main이 관찰한 사실은 `lessons.py note`로 추가하며, `lessons.py pending`으로 대기 항목을 조회하거나 닫고 `lessons.py brief`로 교훈 작성 요청 초안을 만듭니다.
- 사용자가 통합을 요청하면 근거가 충분한 교훈을 프로젝트 규칙에 반영합니다. 교훈 기록이 스스로 규칙이 되지는 않습니다.

## VS Code 확장

![Agent Factory VS Code 확장: 컨트롤센터가 도메인별 수행자와 한 실행의 수행보고서를 보여 줍니다.](https://raw.githubusercontent.com/KoreanLeeChangHyun/agent-factory-vscode-extension/main/static/images/readme-control-center.png)

- 이 플러그인은 단독으로 설치하고 사용할 수 있으며, VS Code 확장은 선택 사항입니다.
- Agent Factory VS Code 확장을 사용하려면 동일한 시맨틱 기본 버전의 플러그인이
  설치되고 활성화되어 있어야 합니다. 예를 들어 확장 `{{version}}`은
  플러그인 `{{version}}+codex.<token>`을 허용합니다.
- 확장은 활성화 시 플러그인의 설치·활성화 상태와 버전을 확인하고, 필요한 경우 자동 설치를 시도합니다.
  호환되는 플러그인이 이미 활성화되어 있으면 추가 설치 없이 사용합니다.
  설치 후에도 호환 상태를 확인할 수 없으면 확장 활성화를 중단하고 오류를 안내합니다.
- 직접 설치하거나 업데이트하려면 아래 [수동 설치](#수동-설치)를 참고하십시오.

## 독립 구성 요소

- **익스텐션 + 플러그인:** Agent Factory MCP 없이 완전한 로컬 작업 흐름을 제공합니다. MCP 패키지, 서버, 계정, 테넌트, 연결, 인증된 리소스가 필요하지 않습니다. 플러그인은 익스텐션 없이도 사용할 수 있습니다.
- **MCP:** 별도로 설치·운영하는 서비스이며 로컬 플러그인의 필수 의존성이 아닙니다.

## 스킬

플러그인은 네 가지 공개 스킬을 제공합니다.

- [Agent](skills/agent/SKILL.md): Work·Verification 에이전트에 작업을 위임하고,
  세션과 실행 진행 상황 및 결과를 관리합니다.
- [Convention](skills/convention/SKILL.md): 소통, 사용자 의사결정, 개발, 테스트,
  조사와 인터뷰에 적용하는 공통 규칙을 제공합니다.
- [Document](skills/document/SKILL.md): 프로젝트 문서의 작성·분류·저장·검색을 안내하고,
  프로젝트 명세 문서를 Codex 스킬로 동기화합니다.
- [Tool](skills/tool/SKILL.md): 플러그인 스크립트 목록과 각 스크립트의 규칙을 담당하는 스킬을 안내합니다.

### 에이전트 실행 방식

- 메시지별로 Work(작업 위임), Plan(계획만 작성), Verification(기존 작업 검증)을 선택하거나
  Plan·Work, Work·Verification, Plan·Work·Verification으로 조합할 수 있습니다. 선택은 해당 메시지에만
  적용됩니다. 자세한 절차는 [실행 방식 안내](skills/agent/references/execution-modes.md)를 참고하십시오.
- 작업 유형과 호스트가 감지한 모델 목록이 있으면 `loop.py start --model-catalog-file`이 프로젝트 런타임 루트에
  사용자가 둔 `model-affinity.json`을 기준으로 Work 모델을 추천합니다. 감지된 모델만 고르고, 사용자가 지정한 모델은
  유지합니다. 자세한 내용은 [작업 배분](skills/agent/references/task-allocation.md#model-recommendation)을 참고하십시오.
- 실행 중이거나 멈춘 루프의 Work 작업을 `loop.py handoff`로 다른 에이전트·모델에 넘길 수 있습니다.
  새 세션은 원래 요청, 진행 상황, 변경 경로, 남은 작업과 미해결 지적 사항을 받으며, 이전 세션은 읽기 전용으로 남습니다.
  공급자의 사용량 한도로 작업이 멈추면 Main이 직접 다른 공급자로 넘기고(`--trigger quota`) 사용자에게 알립니다.
- `operation_records.py`는 작업·세션·결정·관찰·교훈 기록을 읽기 전용으로 조회(`query`)하고 단어로 검색(`search`)하며,
  검색 결과마다 출처를 표시합니다. `supervise`는 끝나지 않은 루프를 정상·지연·정체·결정 필요로 판정합니다.

### 모델과 세션

- 에이전트를 Claude Code로도 실행할 수 있습니다. `claude-opus-5-5`, `claude-sonnet-5-5`,
  `claude-fable-5-1`, `claude-haiku-5-5` 같은 `claude-*` 모델이나 `claude-opus`,
  `claude-sonnet`, `claude-haiku` 별칭을 선택하십시오.
- 모든 실행 방식을 Claude로 사용할 수 있습니다. Plan은 Claude의 plan 모드로 계획한 뒤 같은 세션에서 실행합니다.
  실행 권한은 가장 가까운 Claude 권한 모드로 연결되며 OS 샌드박스가 아닙니다.
  자세한 내용은 [실행 방식 안내](skills/agent/references/execution-modes.md#7-execution-providers)를 참고하십시오.
- Antigravity CLI(`agy`)로 Google AI 구독 모델을 사용해 에이전트를 실행할 수도 있습니다. `gemini-*` 모델은
  Antigravity를 선택하며, 그 밖의 모델은 `antigravity/<id>` 형식으로 지정합니다. Antigravity 실행은 텍스트만 지원합니다.
  자세한 내용은 [Antigravity](skills/agent/references/execution-modes.md#71-antigravity)를 참고하십시오.
- 이 패키지는 Codex로 설치하며, Claude와 Antigravity 실행은 런타임이 시작합니다.
  Claude Code에서는 별도의 [Claude Code 배포판](https://github.com/KoreanLeeChangHyun/agent-factory-claude-plugin)을 설치하십시오.

## 수동 설치

- Agent Factory VS Code 확장을 사용하면 기본적으로 플러그인이 자동 설치됩니다.
- 플러그인을 단독으로 사용하거나 수동으로 설치하려면 다음 명령을 실행합니다.

  ```bash
  codex plugin marketplace add KoreanLeeChangHyun/agent-factory-codex-plugin --ref main
  codex plugin add agent-factory@agent-factory
  ```

- 게시된 업데이트를 설치하려면 다음 명령을 실행합니다.

  ```bash
  codex plugin marketplace upgrade agent-factory
  codex plugin add agent-factory@agent-factory
  ```

- 설치 또는 업데이트 후에는 스킬과 도구를 불러올 수 있도록 새 Codex 스레드를 시작합니다.

## 문서 동기화

- Agent Factory 규칙에 따라 `docs/skills/`에 작성한 프로젝트 명세 문서는
  `.codex/skills/`, `.claude/skills/`, `.agents/skills/`로 동기화되어 Codex, Claude Code, Antigravity에서 프로젝트 스킬로 활용됩니다.
- 에이전트는 `docs/skills/`의 프로젝트 명세 문서를 작성·수정·삭제한 후
  [Document 스킬의 동기화 스크립트](skills/document/references/host-sync.md#continuous-codex-synchronization)를
  실행하고 결과를 확인합니다.
- 동기화된 문서를 별도로 편집한 경우 충돌을 보고하고 동기화를 중단하여 변경 내용을 보호합니다.
- 동기화 관리 대상이 아닌 기존 스킬은 수정하지 않고 보존합니다.
  사용자가 문서 마이그레이션을 명시적으로 요청한 경우에만, 요청한 범위 안에서
  Agent Factory 규칙에 따라 마이그레이션할 수 있습니다.

## 호환성

- **운영체제:** Linux, macOS, 네이티브 Windows 실행을 지원하며, WSL에서는 Linux 요구사항을 충족해야 합니다.
  Windows에서는 Git Bash 등에서 네이티브 Python(python.org 또는 Microsoft Store)을 사용하십시오.
  MSYS2/Cygwin용 Python은 지원하지 않습니다. macOS와 Windows는 실제 사용 환경에서 동작 확인이 필요합니다.
- **Python:** Python 3.10+.
- **Codex:** Codex CLI가 설치되어 있어야 합니다. 실행 전에 필요한 기능과 환경을 확인합니다.
- 환경별 조건과 제한은 [호스트 준비 상태 안내](skills/agent/references/installation.md#host-readiness-and-diagnostics)를 참고하십시오.

## 버그 문의

버그는 [m.leechanghyun@gmail.com](mailto:m.leechanghyun@gmail.com)으로 문의해 주세요.

## 라이선스

MIT 라이선스입니다. [LICENSE](LICENSE)를 참고하십시오.
