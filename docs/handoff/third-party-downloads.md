# Third-Party Runtime Downloads

The handoff package does not redistribute Minecraft/Fabric runtime binaries. `scripts/handoff/setup_minecraft_server.sh` downloads and verifies:

- Minecraft Java server 1.20.1 from Mojang's fixed Piston object URL. Expected SHA-1: `84194a2f286ef7c14ed7ce0090dba59902951553`.
- Fabric Installer 1.1.1 from `maven.fabricmc.net`. Expected SHA-256: `2487a69dd6f9d9c2605265a7142d77c26ab62edc620e6bcf810d581d2ee31b79`.
- Fabric Loader 0.19.3 through the verified Fabric installer.
- Fabric Carpet 1.4.112 for Minecraft 1.20 from the project's GitHub release. Expected SHA-256: `00ad0ed15c457fdec0e6eefe84d79e1bb7b8f91f5f3a133cf89cb2b60ffb3d11`.

Minecraft server use is subject to the Minecraft EULA. The setup script requires explicit recipient acceptance and configures a loopback-only, offline-mode development server. Do not expose it to an untrusted network.
