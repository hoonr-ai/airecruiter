"""Backend-driven, resumable Source & Launch ("launch sessions").

One recruiter click is one durable ``launch_sessions`` row; a DB-leased runner
executes its stages so a crash, deploy or closed tab never loses the launch.
``rules`` holds the Step-5 launch rules shared with the browser
(apps/web/lib/launch-rules.ts). Keep this module import-free so importing a
submodule never pulls in the database or the routers.
"""
