# Ponytail deploy

Skill [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) —
«ленивый сеньор»: YAGNI, stdlib раньше кода, одна строка вместо пятидесяти.
Разворачивается на обе машины (Mac Mini + MacBook) через этот репозиторий.

## Что куда легло

| Цель | Как подключён | Файл |
|---|---|---|
| Кодинг-CLI (Claude Code, Codex, Hermes, Copilot, Gemini) | официальный marketplace-install | `scripts/install_ponytail.sh` |
| Любой AGENTS.md-агент в этом репо | нативно читается из корня | `AGENTS.md` |
| Python-код-воркер (`dev_worker`) | правила вшиты в системный промпт | `agents/ponytail.py` |

Область действия — **только код-задачи**. На не-код агентов (копирайтер,
дизайнер, ресёрчер, письма) ponytail сознательно не вешается: сам skill
предупреждает «Do NOT use for non-coding requests», иначе он ухудшает вывод.

## Установка на каждой машине

```bash
cd ~/ai-infra            # где склонирован agent-os
git pull
bash scripts/install_ponytail.sh
```

Скрипт идемпотентный: ставит только в те CLI, что реально установлены, повторный
запуск безопасен. OpenCode и Codex дописываются вручную (скрипт подскажет).

Python-воркерам ничего ставить не нужно — правила подхватываются из репозитория
после `git pull` (перезапусти `ai.worker` в launchd, чтобы промпт обновился).

## Проверка

```bash
cd agents && python -m pytest tests/test_ponytail.py -q
```

В кодинг-CLI после установки: `/ponytail-help` (Claude Code / Codex / Hermes).
