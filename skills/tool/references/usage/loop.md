# `loop.py` usage

Generated from `--help` by `distribution/tool_usage.py`; do not edit by hand.
Rules for when and why to run it stay in the owning Skill listed in [SKILL.md](../../SKILL.md).

```text
usage: loop.py [-h] {start,status,reconcile,recover-receipt,skip,drive,close,refresh-progress} ...

positional arguments:
  {start,status,reconcile,recover-receipt,skip,drive,close,refresh-progress}

options:
  -h, --help            show this help message and exit
```

## `start`

```text
usage: loop.py start [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                     [--project-id PROJECT_ID] --task-list-file TASK_LIST_FILE --task-id TASK_ID
                     --request-file REQUEST_FILE --work-agent WORK_AGENT
                     [--task-mode {work,plan-work,work-verification,plan-work-verification}]
                     [--verification-agent VERIFICATION_AGENT] [--codex CODEX]
                     [--sandbox {read-only,workspace-write,danger-full-access}]
                     [--execution-policy-file EXECUTION_POLICY_FILE]
                     [--approval-policy {never,on-request,untrusted,on-failure}]
                     [--network-access | --no-network-access] [--writable-root WRITABLE_ROOT]
                     [--model MODEL] [--work-model WORK_MODEL]
                     [--work-reasoning-effort {none,low,medium,high,xhigh,max}]
                     [--work-execution-mode {cli-default,workspace-write,danger-full-access,bypass}]
                     [--verification-model VERIFICATION_MODEL]
                     [--verification-reasoning-effort {none,low,medium,high,xhigh,max}]
                     [--verification-execution-mode {cli-default,workspace-write,danger-full-access,bypass}]
                     [--work-capability-binding-file WORK_CAPABILITY_BINDING_FILE]
                     [--verification-capability-binding-file VERIFICATION_CAPABILITY_BINDING_FILE]

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --task-list-file TASK_LIST_FILE
  --task-id TASK_ID
  --request-file REQUEST_FILE
  --work-agent WORK_AGENT
  --task-mode {work,plan-work,work-verification,plan-work-verification}
  --verification-agent VERIFICATION_AGENT
  --codex CODEX
  --sandbox {read-only,workspace-write,danger-full-access}
  --execution-policy-file EXECUTION_POLICY_FILE
  --approval-policy {never,on-request,untrusted,on-failure}
  --network-access, --no-network-access
  --writable-root WRITABLE_ROOT
  --model MODEL
  --work-model WORK_MODEL
  --work-reasoning-effort {none,low,medium,high,xhigh,max}
  --work-execution-mode {cli-default,workspace-write,danger-full-access,bypass}
  --verification-model VERIFICATION_MODEL
  --verification-reasoning-effort {none,low,medium,high,xhigh,max}
  --verification-execution-mode {cli-default,workspace-write,danger-full-access,bypass}
  --work-capability-binding-file WORK_CAPABILITY_BINDING_FILE
  --verification-capability-binding-file VERIFICATION_CAPABILITY_BINDING_FILE
```

## `status`

```text
usage: loop.py status [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                      [--project-id PROJECT_ID] --work-agent WORK_AGENT --loop-id LOOP_ID

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --work-agent WORK_AGENT
  --loop-id LOOP_ID
```

## `reconcile`

```text
usage: loop.py reconcile [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                         [--project-id PROJECT_ID]
                         [--sandbox {read-only,workspace-write,danger-full-access}]
                         [--execution-policy-file EXECUTION_POLICY_FILE]
                         [--approval-policy {never,on-request,untrusted,on-failure}]
                         [--network-access | --no-network-access] [--writable-root WRITABLE_ROOT]
                         --work-agent WORK_AGENT --loop-id LOOP_ID

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --sandbox {read-only,workspace-write,danger-full-access}
  --execution-policy-file EXECUTION_POLICY_FILE
  --approval-policy {never,on-request,untrusted,on-failure}
  --network-access, --no-network-access
  --writable-root WRITABLE_ROOT
  --work-agent WORK_AGENT
  --loop-id LOOP_ID
```

## `recover-receipt`

```text
usage: loop.py recover-receipt [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                               [--project-id PROJECT_ID]
                               [--sandbox {read-only,workspace-write,danger-full-access}]
                               [--execution-policy-file EXECUTION_POLICY_FILE]
                               [--approval-policy {never,on-request,untrusted,on-failure}]
                               [--network-access | --no-network-access]
                               [--writable-root WRITABLE_ROOT] --work-agent WORK_AGENT --loop-id
                               LOOP_ID

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --sandbox {read-only,workspace-write,danger-full-access}
  --execution-policy-file EXECUTION_POLICY_FILE
  --approval-policy {never,on-request,untrusted,on-failure}
  --network-access, --no-network-access
  --writable-root WRITABLE_ROOT
  --work-agent WORK_AGENT
  --loop-id LOOP_ID
```

## `skip`

```text
usage: loop.py skip [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                    [--project-id PROJECT_ID] --work-agent WORK_AGENT --loop-id LOOP_ID --actor
                    {main,human} --authorization-reference AUTHORIZATION_REFERENCE
                    --decision-evidence DECISION_EVIDENCE

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --work-agent WORK_AGENT
  --loop-id LOOP_ID
  --actor {main,human}
  --authorization-reference AUTHORIZATION_REFERENCE
  --decision-evidence DECISION_EVIDENCE
```

## `drive`

```text
usage: loop.py drive [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                     [--project-id PROJECT_ID]
                     [--sandbox {read-only,workspace-write,danger-full-access}]
                     [--execution-policy-file EXECUTION_POLICY_FILE]
                     [--approval-policy {never,on-request,untrusted,on-failure}]
                     [--network-access | --no-network-access] [--writable-root WRITABLE_ROOT]
                     --work-agent WORK_AGENT --loop-id LOOP_ID

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --sandbox {read-only,workspace-write,danger-full-access}
  --execution-policy-file EXECUTION_POLICY_FILE
  --approval-policy {never,on-request,untrusted,on-failure}
  --network-access, --no-network-access
  --writable-root WRITABLE_ROOT
  --work-agent WORK_AGENT
  --loop-id LOOP_ID
```

## `close`

```text
usage: loop.py close [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                     [--project-id PROJECT_ID] --work-agent WORK_AGENT --loop-id LOOP_ID --actor
                     {main,human} --authorization-reference AUTHORIZATION_REFERENCE
                     --decision-evidence DECISION_EVIDENCE

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --work-agent WORK_AGENT
  --loop-id LOOP_ID
  --actor {main,human}
  --authorization-reference AUTHORIZATION_REFERENCE
  --decision-evidence DECISION_EVIDENCE
```

## `refresh-progress`

```text
usage: loop.py refresh-progress [-h] [--project-root PROJECT_ROOT] [--runtime-home RUNTIME_HOME]
                                [--project-id PROJECT_ID] --work-agent WORK_AGENT --loop-id
                                LOOP_ID

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --runtime-home RUNTIME_HOME
  --project-id PROJECT_ID
  --work-agent WORK_AGENT
  --loop-id LOOP_ID
```
