from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def playbook(name):
    play = yaml.safe_load((ROOT / "ansible/playbooks" / name).read_text())[0]
    defaults = {}
    for task in play.get("pre_tasks", []):
        role = task.get("ansible.builtin.import_role", {})
        if role.get("defaults_from"):
            path = ROOT / "ansible/roles" / Path(role["name"]).name / "defaults" / (role["defaults_from"] + ".yml")
            defaults.update(yaml.safe_load(path.read_text()))
    play["vars"] = {**defaults, **play.get("vars", {})}
    return play


def boolean(value):
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "yes", "on", "1")
