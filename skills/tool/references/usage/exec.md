# `exec.py` usage

Generated from `--help` by `distribution/tool_usage.py`; do not edit by hand.
Rules for when and why to run it stay in the owning Skill listed in [SKILL.md](../../SKILL.md).

```text
usage: exec.py [-h]
               {init,location,projects,rebind,map-path,doctor,announce-tasks,submit,send,status,result,cancel,capabilities,goal,worktree,list,reset-conversation,inbox,reconcile,_worker,_bootstrap}
               ...

positional arguments:
  {init,location,projects,rebind,map-path,doctor,announce-tasks,submit,send,status,result,cancel,capabilities,goal,worktree,list,reset-conversation,inbox,reconcile,_worker,_bootstrap}
    doctor              Inspect host sandbox prerequisites; use doctor --help for options
    announce-tasks      Prepare one Main-owned task list for presentation and dispatch
    worktree            Manage a conversation's isolated Git worktree
    reset-conversation  Start a fresh provider conversation while preserving the Agent and run
                        history
    _worker             ==SUPPRESS==
    _bootstrap          ==SUPPRESS==

options:
  -h, --help            show this help message and exit
```

## `init`

```text
usage: exec.py init [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                    [--project-id PROJECT_ID]

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
```

## `location`

```text
usage: exec.py location [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                        [--project-id PROJECT_ID]

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
```

## `projects`

```text
usage: exec.py projects [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                        [--project-id PROJECT_ID]

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
```

## `rebind`

```text
usage: exec.py rebind [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                      [--project-id PROJECT_ID] --from-root FROM_ROOT

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --from-root FROM_ROOT
```

## `map-path`

```text
usage: exec.py map-path [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                        [--project-id PROJECT_ID] --path PATH

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --path PATH
```

## `doctor`

```text
usage: exec.py [-h] [--codex CODEX] [--probe]

Read-only host diagnostics; never change or weaken the requested sandbox.

options:
  -h, --help     show this help message and exit
  --codex CODEX
  --probe        Run a bounded, network-isolated system bubblewrap probe
```

## `announce-tasks`

```text
usage: exec.py announce-tasks [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                              [--project-id PROJECT_ID] --task-list-file TASK_LIST_FILE

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --task-list-file TASK_LIST_FILE
```

## `submit`

```text
usage: exec.py submit [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                      [--project-id PROJECT_ID] [--task-list-file TASK_LIST_FILE]
                      [--task-id TASK_ID]
                      [--request-file REQUEST_FILE | --message MESSAGE | --input-file INPUT_FILE]
                      [--actor {main,human}] [--human-approval-policy {required,bypass}]
                      [--task-mode {direct,work,plan,verification,plan-work,work-verification,plan-work-verification}]
                      [--model MODEL] [--provider {codex,claude}]
                      [--reasoning-effort {none,minimal,low,medium,high,xhigh,max,ultra}]
                      [--agent-permissions AGENT_PERMISSIONS] [--fast | --no-fast]
                      [--goal-mode | --no-goal-mode] [--goal-objective GOAL_OBJECTIVE]
                      [--receipt-request-hash RECEIPT_REQUEST_HASH]
                      [--verified-work-run-id VERIFIED_WORK_RUN_ID] [--dispatch-id DISPATCH_ID]
                      [--capability-binding-file CAPABILITY_BINDING_FILE] --agent AGENT --role
                      ROLE [--codex CODEX] [--claude CLAUDE]
                      [--sandbox {read-only,workspace-write,danger-full-access}]
                      [--execution-policy-file EXECUTION_POLICY_FILE]
                      [--approval-policy {never,on-request,untrusted,on-failure}]
                      [--network-access | --no-network-access] [--writable-root WRITABLE_ROOT]
                      [--heartbeat-interval HEARTBEAT_INTERVAL]
                      [--heartbeat-timeout HEARTBEAT_TIMEOUT] [--start-timeout START_TIMEOUT]
                      [--turn-timeout TURN_TIMEOUT] [--max-attempts MAX_ATTEMPTS]

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --task-list-file TASK_LIST_FILE
  --task-id TASK_ID
  --request-file REQUEST_FILE
  --message MESSAGE
  --input-file INPUT_FILE
                        versioned agent-input JSON with sibling, file-backed images
  --actor {main,human}
  --human-approval-policy {required,bypass}
                        Main delegation approval policy; omitted sends preserve the session policy
  --task-mode {direct,work,plan,verification,plan-work,work-verification,plan-work-verification}
                        Captured execution route; new Main requests default to direct
  --model MODEL
  --provider {codex,claude}
  --reasoning-effort {none,minimal,low,medium,high,xhigh,max,ultra}
  --agent-permissions AGENT_PERMISSIONS
                        Captured Human-selected role permission overrides as JSON
  --fast, --no-fast
  --goal-mode, --no-goal-mode
  --goal-objective GOAL_OBJECTIVE
                        Native persisted nonempty objective; omitted on send preserves the
                        existing objective
  --receipt-request-hash RECEIPT_REQUEST_HASH
                        SHA-256 identity the role receipt must bind (defaults to this run request)
  --verified-work-run-id VERIFIED_WORK_RUN_ID
                        exact Work run checked by a Verification Agent (required for Verification
                        runs)
  --dispatch-id DISPATCH_ID
                        optional idempotency key: dispatch-[A-Za-z0-9][A-Za-z0-9._:-]{0,127};
                        generated when omitted; reuse the same key only for recovery of the same
                        request
  --capability-binding-file CAPABILITY_BINDING_FILE
                        strict Agent capability/authority/effects binding to preserve in this run
  --agent AGENT
  --role ROLE
  --codex CODEX
  --claude CLAUDE
  --sandbox {read-only,workspace-write,danger-full-access}
  --execution-policy-file EXECUTION_POLICY_FILE
  --approval-policy {never,on-request,untrusted,on-failure}
  --network-access, --no-network-access
  --writable-root WRITABLE_ROOT
  --heartbeat-interval HEARTBEAT_INTERVAL
  --heartbeat-timeout HEARTBEAT_TIMEOUT
  --start-timeout START_TIMEOUT
  --turn-timeout TURN_TIMEOUT
  --max-attempts MAX_ATTEMPTS
```

## `send`

```text
usage: exec.py send [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                    [--project-id PROJECT_ID] [--task-list-file TASK_LIST_FILE]
                    [--task-id TASK_ID]
                    [--request-file REQUEST_FILE | --message MESSAGE | --input-file INPUT_FILE]
                    [--actor {main,human}] [--human-approval-policy {required,bypass}]
                    [--task-mode {direct,work,plan,verification,plan-work,work-verification,plan-work-verification}]
                    [--model MODEL] [--provider {codex,claude}]
                    [--reasoning-effort {none,minimal,low,medium,high,xhigh,max,ultra}]
                    [--agent-permissions AGENT_PERMISSIONS] [--fast | --no-fast]
                    [--goal-mode | --no-goal-mode] [--goal-objective GOAL_OBJECTIVE]
                    [--receipt-request-hash RECEIPT_REQUEST_HASH]
                    [--verified-work-run-id VERIFIED_WORK_RUN_ID] [--dispatch-id DISPATCH_ID]
                    [--capability-binding-file CAPABILITY_BINDING_FILE] --agent AGENT
                    [--sandbox {read-only,workspace-write,danger-full-access}]
                    [--execution-policy-file EXECUTION_POLICY_FILE]
                    [--approval-policy {never,on-request,untrusted,on-failure}]
                    [--network-access | --no-network-access] [--writable-root WRITABLE_ROOT]

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --task-list-file TASK_LIST_FILE
  --task-id TASK_ID
  --request-file REQUEST_FILE
  --message MESSAGE
  --input-file INPUT_FILE
                        versioned agent-input JSON with sibling, file-backed images
  --actor {main,human}
  --human-approval-policy {required,bypass}
                        Main delegation approval policy; omitted sends preserve the session policy
  --task-mode {direct,work,plan,verification,plan-work,work-verification,plan-work-verification}
                        Captured execution route; new Main requests default to direct
  --model MODEL
  --provider {codex,claude}
  --reasoning-effort {none,minimal,low,medium,high,xhigh,max,ultra}
  --agent-permissions AGENT_PERMISSIONS
                        Captured Human-selected role permission overrides as JSON
  --fast, --no-fast
  --goal-mode, --no-goal-mode
  --goal-objective GOAL_OBJECTIVE
                        Native persisted nonempty objective; omitted on send preserves the
                        existing objective
  --receipt-request-hash RECEIPT_REQUEST_HASH
                        SHA-256 identity the role receipt must bind (defaults to this run request)
  --verified-work-run-id VERIFIED_WORK_RUN_ID
                        exact Work run checked by a Verification Agent (required for Verification
                        runs)
  --dispatch-id DISPATCH_ID
                        optional idempotency key: dispatch-[A-Za-z0-9][A-Za-z0-9._:-]{0,127};
                        generated when omitted; reuse the same key only for recovery of the same
                        request
  --capability-binding-file CAPABILITY_BINDING_FILE
                        strict Agent capability/authority/effects binding to preserve in this run
  --agent AGENT
  --sandbox {read-only,workspace-write,danger-full-access}
  --execution-policy-file EXECUTION_POLICY_FILE
  --approval-policy {never,on-request,untrusted,on-failure}
  --network-access, --no-network-access
  --writable-root WRITABLE_ROOT
```

## `status`

```text
usage: exec.py status [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                      [--project-id PROJECT_ID] --agent AGENT
                      (--run-id RUN_ID | --dispatch-id DISPATCH_ID)

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --agent AGENT
  --run-id RUN_ID
  --dispatch-id DISPATCH_ID
```

## `result`

```text
usage: exec.py result [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                      [--project-id PROJECT_ID] --agent AGENT --run-id RUN_ID [--ack]

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --agent AGENT
  --run-id RUN_ID
  --ack
```

## `cancel`

```text
usage: exec.py cancel [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                      [--project-id PROJECT_ID] --agent AGENT --run-id RUN_ID

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --agent AGENT
  --run-id RUN_ID
```

## `capabilities`

```text
usage: exec.py capabilities [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                            [--project-id PROJECT_ID] [--codex CODEX] [--claude CLAUDE]
                            [--provider {codex,claude}] [--model MODEL] [--agent AGENT]

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --codex CODEX
  --claude CLAUDE
  --provider {codex,claude}
  --model MODEL
  --agent AGENT
```

## `goal`

```text
usage: exec.py goal [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                    [--project-id PROJECT_ID] --agent AGENT
                    {get,refresh,pause,cancel,clear,disable,resume,reopen}

positional arguments:
  {get,refresh,pause,cancel,clear,disable,resume,reopen}

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --agent AGENT
```

## `worktree`

```text
usage: exec.py worktree [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                        [--project-id PROJECT_ID] --agent AGENT [--changes {reject,keep,copy}]
                        [--path PATH] [--repository REPOSITORY] [--name NAME] [--branch BRANCH]
                        [--base BASE] [--target TARGET] [--codex CODEX] [--claude CLAUDE]
                        [--model MODEL] [--human-approval-policy {required,bypass}]
                        [--sandbox {read-only,workspace-write,danger-full-access}]
                        [--execution-policy-file EXECUTION_POLICY_FILE]
                        [--approval-policy {never,on-request,untrusted,on-failure}]
                        [--network-access | --no-network-access] [--writable-root WRITABLE_ROOT]
                        {status,create,merge,repositories}

positional arguments:
  {status,create,merge,repositories}

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --agent AGENT
  --changes {reject,keep,copy}
  --path PATH
  --repository REPOSITORY
  --name NAME
  --branch BRANCH
  --base BASE
  --target TARGET
  --codex CODEX
  --claude CLAUDE
  --model MODEL
  --human-approval-policy {required,bypass}
  --sandbox {read-only,workspace-write,danger-full-access}
  --execution-policy-file EXECUTION_POLICY_FILE
  --approval-policy {never,on-request,untrusted,on-failure}
  --network-access, --no-network-access
  --writable-root WRITABLE_ROOT
```

## `list`

```text
usage: exec.py list [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                    [--project-id PROJECT_ID]

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
```

## `reset-conversation`

```text
usage: exec.py reset-conversation [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                                  [--project-id PROJECT_ID] --agent AGENT

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --agent AGENT
```

## `inbox`

```text
usage: exec.py inbox [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                     [--project-id PROJECT_ID] [--agent AGENT] [--ack]

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --agent AGENT
  --ack
```

## `reconcile`

```text
usage: exec.py reconcile [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                         [--project-id PROJECT_ID] [--agent AGENT]

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --agent AGENT
```
