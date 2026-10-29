"""Operator tools. Run on the server:  docker compose exec verticlip-saas python -m app.admin <command>

  reports                     list open copyright and abuse reports
  takedown <project_id> [report_id]
                              remove a project, strike its owner (3 strikes suspends the account), email the owner
  suspend <email> / unsuspend <email>
  delete-user <email>         delete an account and all its data
  close-report <report_id>    mark a report handled without action
  testimonials                list customer quotes waiting for approval
  approve <id> / reject <id>  publish a quote on the website, or decline it
  incident open <degraded|outage|maintenance> "<title>" ["<details>"]
  incident update <id> "<details>"   |   incident resolve <id>   |   incident list
"""
from __future__ import annotations

import shutil
import sys

from . import config, db, mailer, pipeline

STRIKE_LIMIT = 3


def _user(email: str) -> dict:
    u = db.get_user_by_email(email.strip().lower())
    if not u:
        sys.exit(f"No account for {email}")
    return u


def takedown(project_id: str, report_id: str = "") -> str:
    with db.conn() as c:
        row = c.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row:
        return f"No project {project_id}"
    owner = db.get_user(row["user_id"])
    db.delete_project(project_id, row["user_id"])
    shutil.rmtree(pipeline.project_dir(project_id), ignore_errors=True)
    msg = f"Removed project {project_id}."
    if owner:
        strikes = owner["strikes"] + 1
        suspend = strikes >= STRIKE_LIMIT
        db.update_user(owner["id"], strikes=strikes, suspended=int(suspend or owner["suspended"]))
        body = ("A project in your Verticlip account was removed after we received a report that it uses content "
                "you may not have the right to use.\n\n"
                f"This is strike {strikes} of {STRIKE_LIMIT}. Accounts with {STRIKE_LIMIT} strikes are suspended.\n"
                f"If you believe this was a mistake, reply to {config.CONTACT_EMAIL}.")
        try:
            mailer.send(owner["email"], "A project was removed from your Verticlip account", body)
        except Exception as exc:
            msg += f" (could not email the owner: {exc})"
        msg += f" Owner now has {strikes} strike(s){' and is suspended' if suspend else ''}."
    if report_id:
        db.set_report_status(report_id, "actioned")
    return msg


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    if cmd == "reports":
        for r in db.list_reports():
            print(f"{r['id']}  {r['kind']:9}  {r['url']}\n    from {r['name']} <{r['email']}>\n    {r['details'][:300]}\n")
    elif cmd == "takedown" and args:
        print(takedown(args[0], args[1] if len(args) > 1 else ""))
    elif cmd in ("suspend", "unsuspend") and args:
        u = _user(args[0])
        db.update_user(u["id"], suspended=int(cmd == "suspend"))
        if cmd == "suspend":
            db.delete_user_sessions(u["id"])
        print(f"{cmd}ed {u['email']}")
    elif cmd == "delete-user" and args:
        u = _user(args[0])
        for pid in db.delete_user_everything(u["id"]):
            shutil.rmtree(pipeline.project_dir(pid), ignore_errors=True)
        print(f"Deleted {u['email']}")
    elif cmd == "testimonials":
        for t in db.list_testimonials("pending"):
            print(f"{t['id']}  {t['name']}, {t['role'] or '-'}\n    \"{t['quote']}\"\n")
    elif cmd in ("approve", "reject") and args:
        print("done" if db.set_testimonial_status(args[0], "approved" if cmd == "approve" else "rejected") else "no such quote")
    elif cmd == "incident" and args:
        sub, rest = args[0], args[1:]
        if sub == "open" and len(rest) >= 2 and rest[0] in ("degraded", "outage", "maintenance"):
            print("opened incident", db.open_incident(rest[0], rest[1], rest[2] if len(rest) > 2 else ""))
        elif sub == "update" and len(rest) >= 2 and rest[0].isdigit():
            print("updated" if db.update_incident(int(rest[0]), rest[1]) else "no such incident")
        elif sub == "resolve" and rest and rest[0].isdigit():
            print("resolved" if db.resolve_incident(int(rest[0])) else "no open incident with that id")
        elif sub == "list":
            import time as _t
            for i in db.list_incidents(_t.time() - 14 * 86400):
                print(i["id"], i["level"], "open" if i["resolved"] is None else "resolved", i["title"])
        else:
            print(__doc__)
    elif cmd == "close-report" and args:
        print("closed" if db.set_report_status(args[0], "closed") else "no such report")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
