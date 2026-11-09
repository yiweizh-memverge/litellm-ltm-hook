# Demo 操作手册（中文）

按顺序做就行。细节和原理见同目录的 `README.md`。

- **VM-1 = 客户的服务器**：Keycloak、LiteLLM + hook、MemMachine core；phase 1 之后多一个 platform。
- **VM-2 = 员工端**：Open WebUI、Claude Code。
- 你主要在 **VM-2** 和浏览器上操作。VM-1 只在跑脚本和查日志时进去。
- 两台 VM 之间不能互相 ssh。需要在两台之间传东西时，都从**笔记本**中转。

---

## 第一部分：准备（demo 前一天，约 15 分钟）

### 1. 开两台新 VM

规格和安全组按 `README.md` 第 1 节。记下两台的公网 IP，以及 VM-2 的**内网** IP。

phase 1 只能跑一次，所以每次彩排或 demo 都要用新开的 VM。

### 2. 在笔记本上设好变量

之后每个新开的笔记本终端都先粘贴这几行：

```bash
K=~/.ssh/<你的密钥>.pem
V1=ubuntu@<VM-1 公网 IP>
V2=ubuntu@<VM-2 公网 IP>
R=/opt/ltm-platform-env/repo
H=/opt/ltm-platform-env/handoff
```

### 3. 把代码拷到两台 VM

在笔记本上，进入仓库目录（分支 `feat/aws-live-demo`）运行：

```bash
for V in $V1 $V2; do
  ssh -i $K $V 'sudo mkdir -p /opt/ltm-platform-env && sudo chown ubuntu: /opt/ltm-platform-env'
  rsync -az --exclude .venv --exclude __pycache__ --exclude '.*_cache' --exclude .claude \
        -e "ssh -i $K" ./ $V:$R/
done
```

### 4. 在 VM-1 上写好 provider 密钥（只在 VM-1 上输入，不要贴到别处）

```bash
ssh -i $K $V1
umask 077; cat > ~/provider.env      # 粘贴下面 4 行，改成真实值，然后按 Ctrl+D
DEEPINFRA_API_KEY=...
OPENAI_API_KEY=...
DEMO_PASSWORD=...
EXTRA_USERS="yu.xu@memverge.com:<密码>:E13579"
```

- `DEMO_PASSWORD`：admin、alice、bob、carol 共用的 SSO 密码，演示时要当众输入，选个好打的。
- `EXTRA_USERS`：额外的人，格式是 `邮箱:密码:员工号`，多个人用空格隔开。每个人会得到一个 SSO 账号，以及一个个人 LiteLLM key。

### 5. Phase 0，VM-1：搭出"客户现在的样子"（约 5 分钟）

在 VM-1 上运行：

```bash
cd /opt/ltm-platform-env/repo
set -a; . ~/provider.env; set +a
CLIENT_IP=<VM-2 内网 IP> bash deploy/aws/0-baseline/server.sh 2>&1 | tee ~/phase0.log
```

它依次做这些事：
1. 装 Postgres、Java、uv。
2. 装 LiteLLM、Keycloak、MemMachine core（main 377510d）、Qdrant。
3. 生成各种密码。
4. 建数据库。
5. 起 Keycloak，建 realm 和 demo 用户；`EXTRA_USERS` 里的人也一起建。
6. 起 LiteLLM，放入**旧版 hook**。
7. 起 core，并**存一句、搜一句**，确认记忆真的能用。
8. 给 Open WebUI、alice、bob、carol 和 `EXTRA_USERS` 里的人建 key，写进 `keys.env`。

**成功的标志：** 输出里有 `core search: found it` 和 `old hook: memmachine-glm-5.2 answers`，最后打印 `Phase 0, VM-1 is up`。

### 6. 把 phase 0 的交接文件传到 VM-2

在笔记本上运行：

```bash
ssh -i $K $V1 cat $H/client-baseline.env | ssh -i $K $V2 "mkdir -p $H && umask 077 && cat > $H/client-baseline.env"
```

文件里有 VM-1 的 IP、Open WebUI 的 SSO client secret、Open WebUI 的共享 key、Alice 的 key 和 admin 密码。

### 7. Phase 0，VM-2：Open WebUI 和 Claude Code（约 4 分钟）

在笔记本上运行：

```bash
ssh -i $K $V2 "bash $R/deploy/aws/0-baseline/client.sh 2>&1 | tee ~/phase0.log"
ssh -i $K $V1 rm $H/client-baseline.env
ssh -i $K $V2 rm $H/client-baseline.env
```

它做这些事：
- 把 VM-2 的 8180 端口转发到 VM-1 的 Keycloak。
- 装 Open WebUI 并配好 SSO，接上 LiteLLM，用共享 key。
- 让 admin 第一个登录，成为 Open WebUI 管理员。
- 装 Claude Code，并写好 `~/.claude/settings.json`。

**成功的标志：** `admin@corp.example signed in` 和 `Phase 0, VM-2 is up`。

### 8. 把每个人的 key 配进 VM-2 的 Claude Code

在笔记本上运行：

```bash
for who in alice bob carol yuxu; do
  ssh -i $K $V1 bash $R/deploy/aws/show-keys.sh $who | ssh -i $K $V2 bash $R/deploy/aws/claude-as.sh $who --setup
done
```

key 经管道直接从 VM-1 传到 VM-2，屏幕上不会显示。每个人一个配置目录 `~/.claude-<名字>/`。
这一步只是写配置，不调用模型，所以 Carol 和 yu.xu 不会因此被 platform 注册。

**准备到此为止。不要提前跑 phase 1。**

---

## 第二部分：demo 当天

### 0. 开窗口

在笔记本上开：

```bash
# 窗口 A：隧道，整场一直开着
ssh -N -i $K -L 8087:127.0.0.1:8087 -L 8180:127.0.0.1:8180 -L 8100:127.0.0.1:8100 $V2
# 窗口 B：VM-2（员工端）
ssh -i $K $V2
# 窗口 C：VM-1（服务器）
ssh -i $K $V1
# 窗口 D：笔记本本地，用来在两台 VM 之间传文件（先粘贴第一部分第 2 步的变量）
```

浏览器打开 http://127.0.0.1:8087（要用 `127.0.0.1`，不要用 `localhost`）。
每个人用一个**单独的浏览器 profile**，因为 SSO 的登录状态是按浏览器保存的。

### 第 1 幕：升级前（客户现在的问题）

1. **Open WebUI，Alice 登录**（Corp SSO，模型选 `memmachine-glm-5.2`），说：
   *"Our Q1 launch is codenamed Bluefin."*
2. 等约 30 秒。**换 Bob 登录**，问：*"What is our Q1 codename?"*
   → **Bob 知道。** 原因：所有人用同一个 Open WebUI key，旧 hook 按 key 区分人，所以大家共用一份记忆。
3. **Claude Code 以 Alice 身份**（窗口 B）：
   ```bash
   bash /opt/ltm-platform-env/repo/deploy/aws/claude-as.sh alice
   ```
   说：*"Remember: my badge PIN hint is 'heron'."*，然后 `/exit`。
4. **任何人都能读别人的记忆**（窗口 C）：
   ```bash
   . /opt/ltm-platform-env/keys.env
   curl -s http://127.0.0.1:4011/v1/chat/completions -H "Authorization: Bearer $BOB_VIRTUAL_KEY" \
     -H 'Content-Type: application/json' -H 'x-memmachine-org: alice_corp_example' \
     -d '{"model":"memmachine-glm-5.2","messages":[{"role":"user","content":"What do you know about me?"}]}'
   ```
   → 这里用的是 Bob 的 key，但加了一个 header，就读到了 Alice 的内容（PIN hint）。

### 第 2 幕：现场升级（phase 1）

**① VM-1：装 platform、升级 hook**（窗口 C，约 1～2 分钟）

```bash
bash /opt/ltm-platform-env/repo/deploy/aws/1-integrate/server.sh 2>&1 | tee ~/phase1.log
```

可以边跑边讲：
- `i00-install`：装 platform。
- `i01-keycloak`：对客户现有的 SSO **只加不改**。新加一个 `memmachine-platform` 应用，并在 Open WebUI 的登录信息里加上 `employee_id`。
- `i02-platform`：
  - 在 platform 登记这台 LiteLLM，发给它一个 credential。
  - 登记 Open WebUI 的共享 key，配上签名密钥。
  - 登记 admin、alice、bob。Carol 和 yu.xu 故意不登记，留着演示自动注册。
- `i03-upgrade`：
  - 预检三项：platform 在线；credential 有效；新 hook 能加载。任何一项失败都直接退出，什么都没改。
  - 备份旧 hook 和配置，换上新 hook，重启 LiteLLM。起不来会**自动回滚**。
  - 最后用 Alice 的 key 试一句，确认 platform 已把她的 key 关联到她本人。

**成功的标志：** `platform linked Alice's key on its first use` 和 `Phase 1, VM-1 done`。

**② 把签名密钥交给 VM-2**（窗口 D）

```bash
ssh -i $K $V1 cat $H/client-integrate.env | ssh -i $K $V2 "umask 077 && cat > $H/client-integrate.env"
```

文件里只有一行：Open WebUI 给"当前用户是谁"签名用的密钥。只有 Open WebUI 和 platform 持有，LiteLLM 没有。

**③ VM-2：员工端改配置**（窗口 B，约 1 分钟）

```bash
bash /opt/ltm-platform-env/repo/deploy/aws/1-integrate/client.sh
rm /opt/ltm-platform-env/handoff/client-integrate.env
```

- **Open WebUI 加 4 行配置并重启：** 以后每次请求都带上签名的用户身份，并用员工号作为用户名。打印的 diff 里密钥显示为 `<secret>`。
- **Claude Code 加几个 header：** 告诉 hook "我是 Claude Code、项目是 default"。
- **转发 8100：** 让浏览器能打开 portal。
- 改过的文件都备份成了 `*.before-memmachine`。

**④（彩排时做，demo 当天可跳过）端到端检查**（窗口 C）

```bash
bash /opt/ltm-platform-env/repo/deploy/aws/1-integrate/verify.sh 2>&1 | tee ~/verify.log
```

预期最后一行是 `48/48 passed`。

### 第 3 幕：升级后

1. **Alice 在 Open WebUI 里先退出、再登录。** 这样用户名才会变成员工号 E12345。然后说：*"Our Q1 launch is codenamed Bluefin."*
2. 等约 30 秒，**Bob** 问代号 → **Bob 不知道了**：每个人有了自己的记忆。
3. **Claude Code 以 Alice 身份**（窗口 B）：
   ```bash
   bash /opt/ltm-platform-env/repo/deploy/aws/claude-as.sh alice
   ```
   问：*"What is our Q1 codename?"* → **答出 Bluefin**：同一个人，换个工具，记忆是通的。
4. 把第 1 幕第 4 步的 curl 再跑一次（窗口 C）。header 保持原样，或者改成 Alice 的员工号 `x-memmachine-org: E12345`，都一样 → **读不到 Alice 的了**：platform 按 key 验证出来的人（Bob，E67890）决定用哪份记忆，这个 header 不起作用。
5. **Portal**：http://127.0.0.1:8100/portal/
   - 用 Alice 登录：Memories 页能看到 Open WebUI 和 Claude Code 两边存的内容；还可以把项目共享给 Bob。
   - **Carol** 第一次在 Open WebUI 聊天 → 自动注册。用 admin 登录 portal，在 Admin 页能看到她。
   - Admin 页关掉 Alice 的记忆 → 她下一次聊天就没有记忆了 → 看审计日志 → 再打开。
6. （可选）**yu.xu**：SSO 密码是 `EXTRA_USERS` 里设的那个。Claude Code 用：
   ```bash
   bash /opt/ltm-platform-env/repo/deploy/aws/claude-as.sh yuxu
   ```

### 第 4 幕（可选）：单独装一套 MemMachine 要多久

见 `README.md` 第 7 节。先在 VM-1 上建一个只能用两个模型的 key，然后在 VM-2 上跑 `2-standalone/memmachine.sh`，最后会打印每一步的耗时。
（这一步还没在 VM 上实际跑过。）

---

## 常用命令

| 想做什么 | 在哪 | 命令 |
|---|---|---|
| 看所有人的 key | VM-1 | `bash /opt/ltm-platform-env/repo/deploy/aws/show-keys.sh` |
| 只看某个人的 key | VM-1 | `bash /opt/ltm-platform-env/repo/deploy/aws/show-keys.sh bob` |
| 以某人身份开 Claude Code（每次都是新会话、新的空目录） | VM-2 | `bash /opt/ltm-platform-env/repo/deploy/aws/claude-as.sh bob` |
| 问一句就退出 | VM-2 | `bash .../claude-as.sh bob -p "What is my badge PIN hint?"` |
| 重新输入某人的 key | VM-2 | `bash .../claude-as.sh bob --key` |
| SSO 密码 | VM-1 | `cat /opt/ltm-platform-env/accounts.txt`（`EXTRA_USERS` 的人用 `provider.env` 里的密码） |
| platform 里登记了谁 | VM-1 | `cd /opt/ltm-platform-env/repo && /opt/ltm-platform-env/platform/.venv/bin/python -m mock_platform.admin --db /opt/ltm-platform-env/platform/registry.sqlite user list` |
| platform 放行或拒绝了什么 | VM-1 | `journalctl -u ltmplat-platform -f \| grep decision=` |
| hook 在做什么 | VM-1 | `journalctl -u ltmplat-litellm -f \| grep "MemMachine trace"` |
| core 的日志 | VM-1 | `journalctl -u ltmplat-memmachine -f` |

**Claude Code 小提示**
- 会话里用 `/status` 查看当前模型，用 `/exit` 退出。
- 演示记忆时，**写入和提问一定要在两个不同的会话里**。在同一个会话里，模型本来就能看到前面的对话，答对了也说明不了问题。
- 每个人第一次启动交互模式时，可能会先出现几个初次使用的提示（主题、是否信任文件夹）。按回车接受默认即可，信任文件夹选 Yes。
- 运行时出现 `unrecognized_model` 只是警告，可以忽略。

## 出问题时

- **phase 1 的 VM-1 脚本中途报错：** 预检阶段失败的话，什么都没改。升级失败的话，脚本会自动回滚到旧 hook。修好后可以重跑，已完成的步骤会跳过。
- **手动撤销 hook 升级：**
  ```bash
  cd /opt/ltm-platform-env/litellm && B=$(ls -d backup-* | tail -1)
  rm -rf memmachine_adapters        # 新 hook 带来的目录，旧 hook 没有
  cp -a $B/memmachine_hook.py $B/config.yaml . && install -m 600 $B/litellm.env ../litellm.env
  sudo systemctl restart ltmplat-litellm
  ```
- **手动撤销 Open WebUI 的改动：** 用 `/opt/ltm-platform-env/openwebui.env.before-memmachine` 覆盖 `openwebui.env`，然后运行 `sudo systemctl restart ltmplat-openwebui`。
- **浏览器打不开页面：** 先检查窗口 A 的隧道还在不在，以及地址是不是 `127.0.0.1`。
- **记忆没生效：** 看上表里 platform 和 hook 的日志。`decision=deny` 后面会写明原因。
