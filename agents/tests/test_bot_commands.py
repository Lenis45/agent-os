import bot_commands


def test_orchestrator_commands_are_native_telegram_safe():
    commands = bot_commands.ORCHESTRATOR_COMMANDS
    names = [name for name, _ in commands]

    assert {"start", "help", "agents", "tickets", "leads", "content", "calendar", "clear"} <= set(names)
    assert all(name.islower() and name.replace("_", "").isalnum() for name in names)
    assert all(len(description) <= 256 for _, description in commands)


def test_support_commands_are_native_telegram_safe():
    commands = bot_commands.SUPPORT_COMMANDS
    names = [name for name, _ in commands]

    assert {"start", "help", "status", "contact"} <= set(names)
    assert all(name.islower() and name.replace("_", "").isalnum() for name in names)
    assert all(len(description) <= 256 for _, description in commands)


def test_secretary_commands_are_native_telegram_safe():
    commands = bot_commands.SECRETARY_COMMANDS
    names = [name for name, _ in commands]

    assert {"start", "help", "save", "translate", "team"} <= set(names)
    assert all(name.islower() and name.replace("_", "").isalnum() for name in names)
    assert all(len(description) <= 256 for _, description in commands)


def test_help_text_matches_registered_commands():
    orchestrator_help = bot_commands.command_menu_text("orchestrator")
    support_help = bot_commands.command_menu_text("support")

    for name, _ in bot_commands.ORCHESTRATOR_COMMANDS:
        if name != "start":
            assert f"/{name}" in orchestrator_help
    for name, _ in bot_commands.SUPPORT_COMMANDS:
        if name != "start":
            assert f"/{name}" in support_help
    secretary_help = bot_commands.command_menu_text("secretary")
    for name, _ in bot_commands.SECRETARY_COMMANDS:
        if name != "start":
            assert f"/{name}" in secretary_help
