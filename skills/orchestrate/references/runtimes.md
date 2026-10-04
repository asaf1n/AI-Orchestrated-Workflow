# Способы вызова

Протокол один. Меняется только форма вызова навыка.

| Среда | Форма | Куда ставятся роли |
|---|---|---|
| Claude Code | `/orchestrate` и дальше текст команды | Навык — `.claude/skills/orchestrate/` или общий `~/.agents/skills/orchestrate/`. Роли — `.claude/agents/implementer.md` и `.claude/agents/reviewer.md`. В метаданных роли `model: inherit`. |
| Codex | `$orchestrate` и тот же текст команды | Навыки — `~/.agents/skills/` или `.agents/skills/`. Роли — `.codex/agents/implementer.toml` и `.codex/agents/reviewer.toml` без поля модели. |

Неявный запуск выключен: у навыка Claude стоит `disable-model-invocation: true`, у описания Codex — `allow_implicit_invocation: false`.

Исполнитель и ревьювер каждый раз получают новый контекст. Уже открытый инструмент мог не перечитать определения ролей; новая роль начинается по файлам, которые лежат на диске сейчас.

Текст трёх команд — в [session-start.md](session-start.md).
