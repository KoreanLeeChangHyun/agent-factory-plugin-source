# Artifacts

## 1. Storage and delivery

- Use `<project-root>/docs/artifact/` for AI-created files outside Document packages:
  HTML previews or interactive explanations, SVG illustrations, screenshots,
  generated images and other task deliverables. Create it when an output is needed.
- `docs/artifact/` is a directory, not a Document type or an active Skill. Files need no
  `SKILL.md`, YAML metadata, document category or mandatory package structure.
- Prefer `docs/artifact/<task-or-topic>/` when related files belong together. Keep HTML,
  SVG, images and supporting files together with working relative links; use clear
  filenames and preserve existing outputs unless replacement is in scope.
- Follow the Human's explicit output location when supplied. Keep application source
  in its owning source tree and canonical Documents in their Document locations.
  Assets owned by a Document remain in that Document's `assets/` directory.
- Keep runtime state, logs and disposable execution scratch files in the producing
  run's storage. Use `docs/artifact/` for outputs intended to be inspected or reused.
- Artifact files are not automatically cataloged as Documents, synchronized to
  `.codex/skills/`, `.claude/skills/`, published or committed. Preserve the task's existing permissions.
- Verify the output using checks appropriate to its format and provide a clickable
  file link in the result. Distinguish a saved file from a rendered or visually checked
  result; do not claim browser or image inspection unless it occurred.
