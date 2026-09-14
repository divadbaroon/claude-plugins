# Loopback health and rejected repair proposals

Assessed service startup now verifies listening sockets against the launched process and its descendants, using psutil and a bounded macOS lsof fallback when access is restricted. It tries concrete IPv4/IPv6 loopback addresses at the expected port and route, and records the reachable health URL. An unrelated healthy listener is not accepted. Inaccessible ownership evidence causes health to remain unverified. A running process whose HTTP health never succeeds is reported separately from a process exit.

Repair service commands may forward only --host (127.0.0.1 or ::1), --port (1024–65535), and --strictPort through npm run to a script directly invoking vite. Wrappers, unknown options, duplicate flags, and non-loopback host values are rejected. The bounded argument cap is twelve. The agent policy documents these same options.

A proposal rejected by command/plan validation records a validation failure, rejected command inputs, and the precise error. That evidence is sent to Setup for correction within the existing five-repair budget. Repeating rejected command inputs terminates recovery. No rejected command executes. Other terminal cases such as unavailable model providers or explicit unsupported/input responses remain terminal. This is not unrestricted shell execution or general coordinated port rewriting.

The Soil Science viewer was rerun from its accepted plan and reached HTTP health on [::1]:5173. Its missing VITE_PUBLIC_API_URL remains a separate application configuration issue. Prior failed records were preserved; a separate verification run exercised the accepted initial plan after repairing the host validator.
