STANDING RULES
- Read the current code on main before changing anything. If it contradicts a decision above, stop and
  tell me rather than improvising.
- steam-backlog-triage, steam-wishlist-deals, steam-deck-ha-bridge: Python standard library only; each page
  is one HTML file with no build step; Alpine with OpenRC and BusyBox crond is the primary target.
  mcp-server-steam-backlog: follow its CLAUDE.md; shared files change only in mcp-server-shared.
- Everything committed AND everything in a PR title, body or comment is public. Never write IPs, hostnames,
  my domain, container IDs, my time zone, my SteamID, phone or notify-service names, my real name or
  personal email, my Cloudflare team name or AUD, or any key. Use placeholders (7656119XXXXXXXXXX,
  notify.mobile_app_your_phone, steam.example.com, your-team.cloudflareaccess.com). Grep every diff.
  Author: SDNick484 <SDNick484@users.noreply.github.com>.
- Never ask me to paste keys or tokens into chat. If a step needs one, give me a vim command for the file
  on the server.
- Installers stay safe to re-run: keep config and data, remember options, replace only their own crontab
  block. Update the README in the same PR when behavior or options change.

SHIP PROCEDURE
1. Work on the feature branch named above. Small, focused commits, each with its tests.
2. Before pushing, run everything CI runs, locally, with nothing skipped (REQUIRE_NODE=1,
   REQUIRE_OPENSSL=1, REQUIRE_BROWSER=1 where the repo uses them; the installer check in a
   python:3.12-alpine container if an installer changed; the privacy scan if the repo has one).
3. git push -u origin <branch>, then gh pr create. PR body: what changed, the exact commands you ran with
   pass counts, and anything I must do by hand. No deploy commands and no server details in the PR.
4. gh pr checks <number> --watch --interval 30. If a check fails: gh run view <run-id> --log-failed, fix
   the cause, push, watch again. Never get to green by skipping, loosening or deleting a test, lowering a
   threshold, or changing a pin this prompt didn't ask you to change. After three failed rounds, stop and
   tell me what is failing and why.
5. When every check is green, notify me: use your push-notification tool if you have one; otherwise, if
   STEAM_NOTIFY_CMD is set, run it with one argument, "<repo>#<number> CI green: <title>". Do not merge.
6. Finish with one message, in chat only: the PR URL, the check summary, and a DEPLOY VERDICT. I deploy
   from a dashboard that archives only a fixed list of top-level files per app (no installer run), and
   refuses when the diff touches install/, deploy/ or dependencies. So say which applies, from the diff:
     A  Dashboard deploy is enough: only already-shipped files changed.
     B  Installer by hand: name the paths that changed under install/ or deploy/, or the dependency.
     C  Registry first: list every NEW file that must ship (any top-level .py, .html or .sh the app needs
        at run time), then A or B.
   Then the checks to run after deploying (complete commands, saying where each runs: my laptop via
   ssh root@pve "pct exec 210 -- sh -c '...'" or a shell inside the CT; vim for edits), any file I must
   edit on the server, and whether a tunnel or Access change is needed. Before anything destructive on
   the server, have me take a pct snapshot first.
