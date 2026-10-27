<script setup lang="ts">
import { ref } from 'vue'
import { DocumentCopy } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

const activeTab = ref('cli')
const cliSteps = [
  {
    id: 'cli-install', title: '1. 安装与查看帮助',
    paragraphs: [
      '在执行命令的机器上准备 Python 3.10 或以上版本，以及可登录 C-Check 的平台账号。CLI 通过现有 API 工作，本机无需 GPU、Docker 或模型权重。',
      '以下使用已部署的 rc.19 安装目录。先运行 ccheck-review help；若提示找不到命令，再执行安装命令。其他版本请替换实际发布目录。',
    ],
    command: String.raw`python3 --version
sudo install -m 755 \
  /home/lyh/c-check-instance/releases/v0.1.0-rc.19/cli/ccheck-review.pyz \
  /usr/local/bin/ccheck-review
command -v ccheck-review
ccheck-review --version
ccheck-review help
ccheck-review help submit`,
    notes: ['只有复制到系统命令目录需要 sudo。后续登录、提交和查询使用同一个普通系统用户，避免切换到另一份 CLI 配置。'],
  },
  {
    id: 'cli-login', title: '2. 登录平台账号',
    paragraphs: [
      '示例服务地址为 http://172.26.2.250:18800，请替换为自己的 Web 服务地址。不要填写 /workspace 页面地址或模型推理端口。admin 是本次示例账号，也可以换成已有普通账号。',
      '执行后在 Password 提示处输入平台密码，输入不回显。CLI 与浏览器需要分别登录；CLI 登录成功后会保存服务地址和访问令牌，不保存密码。',
    ],
    command: String.raw`ccheck-review --server http://172.26.2.250:18800 \
  --allow-insecure-http login --username admin`,
    notes: [
      '该示例使用内网 HTTP。--allow-insecure-http 显式允许明文连接，账号信息、令牌和代码在传输中不加密；有 HTTPS 时优先使用 HTTPS。',
      '默认 CLI 配置为 ~/.config/ccheck-review/config.json（支持 XDG_CONFIG_HOME）。它不是部署文件 c-check-site.json。成功登录后，同一用户可省略 --server 和已保存的 HTTP 选项；下面保留完整地址便于核对。',
    ],
  },
  {
    id: 'cli-models', title: '3. 确认模型与检查类别',
    paragraphs: ['models 列出模型节点 ID，default=True 表示当前默认模型。不传 --model 时使用默认模型，不传 --checks 时检查全部六类。'],
    command: String.raw`ccheck-review --server http://172.26.2.250:18800 \
  --allow-insecure-http models
ccheck-review --server http://172.26.2.250:18800 \
  --allow-insecure-http checks`,
    notes: ['--model 使用 models 返回的节点 ID，不是模型名称。--checks 使用 checks 返回的类别键，多个类别用英文逗号分隔。'],
  },
  {
    id: 'cli-submit', title: '4. 将整个目录提交为一个任务',
    paragraphs: [
      '--dir 指向运行 CLI 的机器上的目录。以下将 E5_Wireless_txt 中符合条件的文件递归收集为一个任务，子目录也会包含在内。',
      '支持 .c、.h、_c.txt、_h.txt；其他文件忽略。不会按 .gitignore 排除文件，提交前请检查目录。源文件符号链接会被拒绝，目录符号链接不遍历；a.c 与 a_c.txt 等还原后重名需先处理。',
    ],
    command: String.raw`ccheck-review --server http://172.26.2.250:18800 \
  --allow-insecure-http submit \
  --name "WirelessFull_$(date +%Y%m%d_%H%M%S)" \
  --dir /home/lyh/c-check-test-data/E5_Wireless_txt \
  --wait \
  --gpu`,
    notes: [
      '提交后记录输出的任务 ID。--wait 持续查询直至结束，交互终端显示实时进度条；--gpu 附带服务器 GPU 数据，需要管理员账号，普通账号可去掉。',
      '需要提交后立即返回时，同时去掉 --wait 和 --gpu。CLI 提交的任务也会出现在同一账号的 Web 页面。',
      '上传仍受服务端文件数、文件大小和 ZIP 解压总量限制；超限时会报错，不会自动拆成多个任务。',
    ],
  },
  {
    id: 'cli-status', title: '5. 查看进度或恢复等待',
    paragraphs: [
      '下面的 UUID 是这次 WirelessFull 任务的示例 ID，请替换为自己提交时返回的任务 ID。status 后必须提供任务 ID。',
      'Ctrl+C 只停止本地等待，不会取消后台任务。网络中断后可以用同一 ID 继续查询；若提交响应丢失，先用 list 按名称查找，避免重复提交。',
    ],
    command: String.raw`ccheck-review --server http://172.26.2.250:18800 \
  --allow-insecure-http status \
  7251d630-3d69-4662-b7c4-5ad4c02cd476 \
  --wait --gpu --poll-interval 2

ccheck-review --server http://172.26.2.250:18800 \
  --allow-insecure-http list --name WirelessFull --limit 20`,
    output: String.raw`Review [#########---------------]  38.0% running
scan_files=19/32 remaining=13 uploaded=32
VRAM [######################--]  90.6% 43.5/48.0 GiB
GPU  [########################]  98.0%`,
    notes: [
      '以上为进度显示示例，不是当前实时数据。Review 是服务端任务进度；scan_files 是当前扫描计划已完成/总文件数，remaining 包含尚未开始和扫描中的文件。',
      '单个文件可能有多个分片，全部完成才计入完成数。扫描计划文件数可能少于 uploaded；重试或重新分片时计数可能重置。旧任务或未生成计划时可显示 N/A。',
      'remaining=0 后仍可能在整理结果和生成报告，以状态 completed 为准。默认每 5 秒查询一次，--poll-interval 2 改为每 2 秒查询；不加 --wait 时只查询一次。',
    ],
  },
  {
    id: 'cli-gpu', title: '6. 单独查看 GPU 与显存',
    paragraphs: ['管理员可以单独持续查看 GPU 数据，Ctrl+C 退出监控。不加 --watch 时只读取一次。'],
    command: String.raw`ccheck-review --server http://172.26.2.250:18800 \
  --allow-insecure-http gpu --watch --poll-interval 2`,
    notes: [
      'VRAM 是显存，43.5/48.0 GiB 表示已用/总显存；GPU 百分比表示运算利用率。两者都不是审查完成百分比。vLLM 会预分配推理缓存，显存占用高不一定异常。',
      '显示的是 API 能采集的服务器共享 GPU 数据，可能包含 Jina、其他任务或用户的占用，不代表本任务独占。无权限或采集失败时显示 unavailable / N/A，不代表使用率为 0。',
    ],
  },
  {
    id: 'cli-report', title: '7. 下载与查看报告',
    paragraphs: ['任务完成后，使用任务 ID 下载报告，CLI 会自动查找对应报告。下面保留你实际使用过的 PDF 示例；替换任务 ID 后即可用于下一次任务。'],
    command: String.raw`ccheck-review --server http://172.26.2.250:18800 \
  --allow-insecure-http report \
  7251d630-3d69-4662-b7c4-5ad4c02cd476 \
  --output ./WirelessFull-report.pdf`,
    notes: [
      '文件保存在运行 CLI 的机器上，./ 表示当前终端目录。在服务器执行时，PDF 也保存在服务器；也可在 Web 历史报告中查看和下载。',
      '将扩展名换成 .md、.txt 或 .json 可下载对应格式；--format 可显式指定格式。未指定 --output 时默认在终端输出 Markdown；PDF 必须指定输出路径。',
      '输出父目录必须已存在。已有同名文件默认不覆盖，需要覆盖时显式添加 --force。报告未就绪时先查询任务状态，不要重新提交审查。',
    ],
  },
]

const commands = [
  ['help [命令]', '查看总帮助或某条命令的参数，无需登录。'],
  ['login --username 用户名', '交互输入密码并保存当前服务器的登录令牌。'],
  ['logout', '移除本机对应服务器的令牌，不撤销其他客户端会话。'],
  ['models', '列出可用模型节点 ID 与默认模型。'],
  ['checks', '列出全部六类检查键。'],
  ['submit --name 名称 --dir 目录', '将目录递归提交为一个任务，默认提交后返回。'],
  ['status 任务ID', '查询状态，配合 --wait 可持续查看进度。'],
  ['list', '查询近期任务，支持 --name、--limit、--offset。'],
  ['gpu', '查询 GPU，配合 --watch 持续刷新，需管理员权限。'],
  ['report 任务ID', '读取报告，配合 --output 下载到本机。'],
]
const options = [
  ['--server', '服务根地址；优先级为命令参数、CCHECK_REVIEW_SERVER、CLI 配置，最后默认 http://127.0.0.1:8800。'],
  ['--config', '指定 CLI 配置文件，默认 ~/.config/ccheck-review/config.json，支持 XDG_CONFIG_HOME。'],
  ['--model / --checks', 'submit 可选：模型节点 ID / 英文逗号分隔的检查类别，默认使用默认模型和全部六类。'],
  ['--wait / --gpu', 'submit、status 可选：持续等待 / 附带 GPU 数据。组合使用可持续刷新；单独 --gpu 只读取一次。'],
  ['--poll-interval', '轮询间隔秒数，默认 5；用于等待任务或持续监控 GPU。'],
  ['--wait-timeout', '等待任务的总时限（秒），默认不限。超时退出不会取消后台任务。'],
  ['--timeout', '单次 HTTP 请求超时（秒），默认 60；大目录上传或 PDF 渲染可适当增加。'],
  ['--json', '机器可读输出。等待时进度事件写入 stderr，最终结果写入 stdout，不显示动态进度条。'],
]

const serviceActions = [
  {
    name: '查看服务状态',
    command: String.raw`sudo python3 /home/lyh/c-check-packages/release-v0.1.0-rc.16/cli/ccheck.pyz status \
  --config /home/lyh/c-check-site.json`,
    description: '显示部署版本、镜像、验收记录、维护标记及容器状态。判断当前是否运行，请查看 containers 中的 State、Health 等字段；acceptance 是已保存的验收记录，不代表当前服务一定在线。',
  },
  {
    name: '停止服务',
    command: String.raw`sudo python3 /home/lyh/c-check-packages/release-v0.1.0-rc.16/cli/ccheck.pyz stop \
  --config /home/lyh/c-check-site.json`,
    description: '进入维护状态，等待在途审查任务结束后停止本项目容器。停止后 Web 和审查 API 不可用，数据库、模型、上传文件和密钥保留。若提示仍有待处理任务，维护状态可能保留，请先检查任务情况和命令输出。',
  },
  {
    name: '启动服务',
    command: String.raw`sudo python3 /home/lyh/c-check-packages/release-v0.1.0-rc.16/cli/ccheck.pyz start \
  --config /home/lyh/c-check-site.json`,
    description: '校验已部署发布包和模型，依次启动数据服务、推理服务及 Web/API/Worker，并等待健康检查，成功后解除维护状态。模型校验和加载需要时间，请等待命令结束，再执行 status 并打开 Web 页面确认可用。',
  },
]

async function copyCommand(text: string, event: MouseEvent) {
  const code = (event.currentTarget as HTMLElement).closest('.help-command')?.querySelector('code')
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('命令已复制')
  } catch {
    const selection = window.getSelection()
    if (code && selection) {
      const range = document.createRange()
      range.selectNodeContents(code)
      selection.removeAllRanges()
      selection.addRange(range)
      ElMessage.warning('无法自动复制，已选中命令，请手动复制。')
    } else {
      ElMessage.warning('无法自动复制，请选中命令后手动复制。')
    }
  }
}
</script>

<template>
  <section class="help-page">
    <header class="page-header">
      <div><h1>使用帮助</h1><p>通过网页或命令行提交代码、跟踪进度和查看审查报告。</p></div>
    </header>
    <el-tabs v-model="activeTab" class="help-tabs">
      <el-tab-pane label="CLI 使用" name="cli">
        <div class="cli-intro">
          <h2>ccheck-review 命令行指南</h2>
          <p>以 WirelessFull 整目录审查为例，使用现有平台账号完成登录、提交、等待和报告下载。以下命令适用于 Linux Bash，地址、目录、版本和任务 ID 请按实际环境替换。</p>
        </div>
        <nav class="help-contents" aria-label="CLI 帮助目录">
          <a v-for="step in cliSteps" :key="step.id" :href="`#${step.id}`">{{ step.title }}</a>
          <a href="#cli-reference">8. 命令与常见问题</a>
        </nav>
        <section v-for="step in cliSteps" :id="step.id" :key="step.id" class="help-section" :aria-labelledby="`${step.id}-title`">
          <h3 :id="`${step.id}-title`">{{ step.title }}</h3>
          <p v-for="paragraph in step.paragraphs" :key="paragraph">{{ paragraph }}</p>
          <div class="help-command">
            <div class="command-toolbar">
              <span>Bash</span>
              <el-tooltip content="复制命令" placement="top">
                <button type="button" class="command-copy" :aria-label="`复制${step.title}命令`" @click="copyCommand(step.command, $event)">
                  <el-icon aria-hidden="true"><DocumentCopy /></el-icon>
                </button>
              </el-tooltip>
            </div>
            <pre tabindex="0" :aria-label="`${step.title}命令`"><code>{{ step.command }}</code></pre>
          </div>
          <div v-if="step.output" class="help-output">
            <p class="output-label">终端输出示例</p>
            <pre tabindex="0" aria-label="任务进度输出示例"><code>{{ step.output }}</code></pre>
          </div>
          <ul class="help-notes"><li v-for="note in step.notes" :key="note">{{ note }}</li></ul>
        </section>
        <section id="cli-reference" class="help-section" aria-labelledby="cli-reference-title">
          <h3 id="cli-reference-title">8. 命令与常见问题</h3>
          <p>共 10 条子命令。所有命令均以 ccheck-review 开头，用 ccheck-review help report 等方式查看完整参数。</p>
          <dl class="help-reference"><div v-for="[command, description] in commands" :key="command"><dt><code>{{ command }}</code></dt><dd>{{ description }}</dd></div></dl>
          <h4>常用参数</h4>
          <dl class="help-reference"><div v-for="[option, description] in options" :key="option"><dt><code>{{ option }}</code></dt><dd>{{ description }}</dd></div></dl>
          <div class="help-faq">
            <details><summary>登录报参数错误，或登录后仍提示未登录</summary><p>使用 login --username admin，而不是 login admin 或 login --admin。确认使用同一个系统用户和同一个服务地址；sudo、切换用户或更换 --config 会使用不同配置。令牌过期时重新登录。</p></details>
            <details><summary>找不到命令，或连接不到服务</summary><p>用 command -v ccheck-review 检查安装路径。确认 Python 版本、Web 地址和端口；127.0.0.1 指运行 CLI 的这台机器。首次登录必须指定实际服务地址，CLI 不会自动读取 c-check-site.json。</p></details>
            <details><summary>GPU 显示无权限、N/A 或 unavailable</summary><p>GPU 监控需要管理员权限及服务端设备采集支持。普通用户可去掉 --gpu 继续查看任务进度，GPU 采集失败不会取消审查任务。</p></details>
            <details><summary>文件已经扫描完，为什么还没有报告</summary><p>remaining=0 只表示本轮扫描计划完成，后续结果整理和报告生成可能仍在执行。继续查询 status，等待 completed；若为 failed，查看任务错误信息。</p></details>
            <details><summary>如何退出登录，或在脚本中使用 CLI</summary><p>执行 ccheck-review logout 移除本机令牌。脚本可使用 --json 并检查退出码：0 为命令成功，1 为运行错误，2 为参数或配置错误，3 为认证或权限错误，4 为任务失败，5 为等待超时，130 为用户中断。发现代码问题本身不会让 CLI 返回失败。</p></details>
          </div>
        </section>
      </el-tab-pane>
      <el-tab-pane label="服务管理" name="services">
        <section id="service-management" class="help-section" aria-labelledby="service-management-title">
          <h3 id="service-management-title">服务管理（管理员）</h3>
          <p>在部署 C-Check 的 Linux 服务器上，使用具有 sudo 权限的系统账号执行以下命令。ccheck.pyz 是部署管理工具，负责本项目的启动、停止和状态查询；ccheck-review 用于提交和查询审查任务。</p>
          <p>以下保留 rc.16 的路径示例。请使用与当前部署版本配套的工具：例如已升级到 rc.19，可将路径替换为 /home/lyh/c-check-instance/releases/v0.1.0-rc.19/cli/ccheck.pyz。执行前确认该文件存在；不要仅根据下载目录中的旧版本决定使用哪个工具。</p>
          <p>三条命令中的 --config 都指向同一实例的部署配置 /home/lyh/c-check-site.json，与审查 CLI 的登录配置不同。操作只针对该配置对应的 C-Check 项目，不会关闭 Linux 服务器或重启全局 Docker。</p>
          <div v-for="action in serviceActions" :key="action.name" class="service-action">
            <h4>{{ action.name }}</h4>
            <p>{{ action.description }}</p>
            <div class="help-command">
              <div class="command-toolbar">
                <span>Bash</span>
                <el-tooltip content="复制命令" placement="top">
                  <button type="button" class="command-copy" :aria-label="`复制${action.name}命令`" @click="copyCommand(action.command, $event)">
                    <el-icon aria-hidden="true"><DocumentCopy /></el-icon>
                  </button>
                </el-tooltip>
              </div>
              <pre tabindex="0" :aria-label="`${action.name}命令`"><code>{{ action.command }}</code></pre>
            </div>
          </div>
          <p>重启本项目时，先执行 stop，确认成功退出后再执行 start，最后执行 status。若任一步失败，先根据输出排查；不要把这三步不加判断地连续执行。</p>
        </section>
      </el-tab-pane>
      <el-tab-pane label="Web 使用" name="web">
        <div class="help-grid">
          <article class="panel glass"><b>01</b><h2>提交 C 语言代码</h2><p>支持文本粘贴、单个 .c/.h 文件、_c.txt/_h.txt 文件和 ZIP 项目压缩包。系统会自动识别项目中的 C 语言源文件。</p></article>
          <article class="panel glass"><b>02</b><h2>选择检查维度</h2><p>可按需要选择缓冲区/数组越界、释放后使用、野指针/悬空指针、资源泄漏、长度/索引整数风险和严重状态机/协议逻辑六类检查目标。</p></article>
          <article class="panel glass"><b>03</b><h2>查看任务进度</h2><p>提交后可在代码审查页看到当前处理对象、整体进度和剩余文件数量。</p></article>
          <article class="panel glass"><b>04</b><h2>阅读审查报告</h2><p>报告按风险等级展示问题，并提供源代码位置、问题说明与修改建议。历史页支持在线预览 Markdown。</p></article>
        </div>
      </el-tab-pane>
    </el-tabs>
  </section>
</template>

<style scoped>
.help-page, .help-tabs, .help-section { min-width: 0; }
.cli-intro, .help-section { max-width: 1000px; }
.cli-intro h2 { margin: 12px 0 10px; font-size: 20px; }
.help-page p, .help-notes, .help-reference { font-size: 14px; line-height: 1.8; overflow-wrap: anywhere; }
.cli-intro p, .help-section p { margin: 10px 0; }
.help-contents { display: flex; flex-wrap: wrap; justify-content: flex-start; gap: 4px 20px; max-width: 1000px; padding: 14px 0 20px; }
.help-contents a { display: inline-flex; align-items: center; min-height: 44px; color: var(--primary); font-size: 14px; text-underline-offset: 4px; }
.help-section { border-top: 1px solid var(--border); padding: 24px 0; scroll-margin-top: 20px; }
.help-section h3 { margin: 0 0 12px; font-size: 18px; }
.help-section h4 { margin: 24px 0 12px; font-size: 16px; }
.help-command, .help-output { min-width: 0; max-width: 100%; margin: 16px 0; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; background: var(--surface-strong); }
.command-toolbar { display: flex; align-items: center; justify-content: space-between; min-height: 44px; padding: 0 8px 0 16px; border-bottom: 1px solid var(--border); color: var(--muted); font-size: 12px; }
.command-copy { display: inline-grid; place-items: center; width: 44px; height: 44px; padding: 0; border: 0; background: transparent; color: var(--primary); cursor: pointer; font-size: 18px; }
.command-copy:hover { background: rgba(72,139,205,.12); }
.help-page pre { max-width: 100%; margin: 0; padding: 16px; overflow-x: auto; font-size: 13px; line-height: 1.75; tab-size: 2; }
.help-page code { font-family: 'Cascadia Code', Consolas, monospace; }
.help-page pre code { white-space: pre; overflow-wrap: normal; }
.help-page :is(a, button, pre, summary):focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
.help-notes { margin: 12px 0 0; padding-left: 22px; }
.help-notes li + li { margin-top: 8px; }
.help-output .output-label { margin: 0; padding: 12px 16px 0; color: var(--muted); font-size: 12px; }
.help-reference { margin: 16px 0; }
.help-reference > div { display: grid; grid-template-columns: minmax(180px, 32%) minmax(0, 1fr); gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--border); }
.help-reference dt { min-width: 0; font-size: 13px; }
.help-reference dd { margin: 0; }
.help-faq { margin-top: 24px; }
.help-faq details { border-bottom: 1px solid var(--border); padding: 8px 0; }
.help-faq summary { padding: 12px 4px; cursor: pointer; font-size: 14px; font-weight: 600; overflow-wrap: anywhere; }
.help-faq details p { padding: 0 4px 8px; }
@media (max-width: 600px) {
  .help-reference > div { grid-template-columns: minmax(0, 1fr); gap: 4px; }
  .help-contents { gap: 4px 16px; }
  .help-page pre { padding: 12px; }
}
</style>
