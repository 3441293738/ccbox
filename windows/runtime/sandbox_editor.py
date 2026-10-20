"""Native Linux VS Code, including its Webview, runs in the workload container.

Only the RFB picture/input channel reaches the Windows viewer. No host profiles
are imported. Downloads have an explicit loopback proxy and no direct fallback.
"""
import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import threading
from pathlib import Path
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = Path.home() / '.ccbox/sandbox-editor'
PROXY = 'http://127.0.0.1:18080'
EXTENSION = 'anthropic.claude-code'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def environment():
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(('VSCODE_', 'ELECTRON_', 'ANTHROPIC_')) or key in ('SSH_AUTH_SOCK', 'DBUS_SESSION_BUS_ADDRESS'):
            env.pop(key)
    env.update(HTTP_PROXY=PROXY, HTTPS_PROXY=PROXY, http_proxy=PROXY, https_proxy=PROXY,
               NO_PROXY='localhost,127.0.0.1,::1', no_proxy='localhost,127.0.0.1,::1',
               BROWSER='/usr/local/bin/ccbox-browser', DONT_PROMPT_WSL_INSTALL='1',
               DISPLAY=':99', CCBOX_SANDBOX_EDITOR='1', DISABLE_AUTOUPDATER='1',
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', DISABLE_TELEMETRY='1', DO_NOT_TRACK='1')
    # Do not alter the container's independent HOME, language, timezone, or OAuth files.
    return env


def metadata(value):
    url = urlsplit(value.get('url', ''))
    if (url.scheme != 'https' or url.hostname not in
            ('vscode.download.prss.microsoft.com', 'az764295.vo.msecnd.net', 'update.code.visualstudio.com')
            or url.username or url.password or url.port not in (None, 443)):
        raise ValueError('下载地址不属于预期的 VS Code 官方 HTTPS 源。')
    if not re.fullmatch(r'[a-fA-F0-9]{64}', value.get('sha256hash', '')):
        raise ValueError('官方更新清单缺少 SHA-256，已停止下载。')
    if not re.fullmatch(r'\d+\.\d+\.\d+', value.get('productVersion', '')):
        raise ValueError('官方版本号格式异常。')
    return value


def download(url, target, limit=400 * 1024 * 1024, resume=False):
    # No credential string ever appears here; the gateway authenticates upstream.
    args = ['curl', '--fail', '--silent', '--show-error', '--location',
            '--proto', '=https', '--proto-redir', '=https', '--proxy', PROXY, '--noproxy', '',
            '--connect-timeout', '25', '--max-time', '300',
            '--max-filesize', str(limit), '--output', str(target), url]
    if resume:
        args[1:1] = ['--continue-at', '-']
    stop = threading.Event()
    def progress():
        while not stop.wait(20):
            if resume and target.exists():
                print(f'已下载 {target.stat().st_size / 1048576:.1f} MB；中断时自动续传。', flush=True)
    thread = threading.Thread(target=progress, daemon=True)
    thread.start()
    try:
        for attempt in range(8 if resume else 1):
            p = subprocess.run(args, capture_output=True, text=True, timeout=330, env=environment())
            if target.exists() and target.stat().st_size > limit:
                raise RuntimeError('下载内容超过大小上限。')
            if not p.returncode:
                return
            # Retry only this authenticated gateway route, preserving partial data.
            if resume and p.returncode in (5, 6, 7, 18, 28, 35, 52, 55, 56):
                print(f'代理下载中断，第 {attempt+1}/8 次；从已有文件继续，不切换直连。', flush=True)
                time.sleep(2)
                continue
            break
        raise RuntimeError('通过当前代理下载未完成；断点已保留，没有改用直连。请检查代理后重试。' + p.stderr[-350:])
    finally:
        stop.set();thread.join(timeout=1)


def extract(archive, destination):
    root = (Path(destination) / 'VSCode-linux-x64').resolve()
    def confined(path):
        resolved = path.resolve()
        if resolved != root and not resolved.is_relative_to(root):
            raise ValueError('安装包链接指向目录外部。')
        return path
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        if sum(m.size for m in members) > 1600 * 1024 * 1024 or len(members) > 100000:
            raise ValueError('安装包解压大小异常。')
        for member in members:
            name = Path(member.name)
            if name.is_absolute() or '..' in name.parts or not name.parts or name.parts[0] != 'VSCode-linux-x64' or '\\' in member.name:
                raise ValueError('安装包包含越界路径。')
            if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                raise ValueError('安装包包含特殊设备文件。')
            confined(Path(destination)/member.name)
        # Debian's Python may predate tarfile.data_filter. Extract regular files
        # ourselves first, then confined links, never applying owner/setuid bits.
        for member in members:
            path=confined(Path(destination)/member.name)
            if member.isdir():
                path.mkdir(parents=True,exist_ok=True)
            elif member.isfile():
                path.parent.mkdir(parents=True,exist_ok=True)
                with tar.extractfile(member) as source,path.open('xb') as output:
                    shutil.copyfileobj(source,output)
                path.chmod((member.mode & 0o777) | 0o600)
        for member in members:
            if not (member.issym() or member.islnk()):continue
            link=Path(member.linkname)
            if link.is_absolute() or '\\' in member.linkname:
                raise ValueError('安装包包含绝对链接。')
            path=confined(Path(destination)/member.name)
            path.parent.mkdir(parents=True,exist_ok=True)
            target=confined((path.parent if member.issym() else Path(destination))/link)
            if member.issym():os.symlink(member.linkname,path)
            else:os.link(target,path)


def ranges(offset, total, chunk=16 * 1024 * 1024):
    return [(start, min(start+chunk, total)-1) for start in range(offset, total, chunk)]


def download_range(url, target, start, end, total):
    """Bounded CDN ranges keep short-lived residential proxy sessions usable."""
    size=end-start+1
    receipt=target.with_name(target.name+'.json')
    if target.exists() and target.stat().st_size==size and receipt.exists():
        try:
            saved=json.loads(receipt.read_text())
            with target.open('rb') as source:digest=hashlib.file_digest(source,'sha256').hexdigest()
            if saved=={'sha256':digest,'start':start,'end':end,'total':total}:return target
        except (ValueError,OSError):pass
    tmp=target.with_name(target.name+'.partial');headers=target.with_name(target.name+'.headers')
    args=['curl','--fail','--silent','--show-error','--location','--proto','=https','--proto-redir','=https',
          '--proxy',PROXY,'--noproxy','','--connect-timeout','25','--max-time','240',
          '--header','Accept-Encoding: identity','--range',f'{start}-{end}',
          '--max-filesize',str(size),'--dump-header',str(headers),'--output',str(tmp),url]
    for attempt in range(4):
        p=subprocess.run(args,capture_output=True,text=True,timeout=270,env=environment())
        raw=headers.read_text(errors='replace') if headers.exists() else ''
        expected=f'bytes {start}-{end}/{total}'
        received=re.findall(r'^content-range:\s*(.+?)\s*$',raw,re.I|re.M)
        if not p.returncode and received and received[-1]==expected and tmp.stat().st_size==size:
            os.replace(tmp,target);headers.unlink(missing_ok=True)
            with target.open('rb') as source:digest=hashlib.file_digest(source,'sha256').hexdigest()
            write_json(receipt,{'sha256':digest,'start':start,'end':end,'total':total})
            return target
        if not p.returncode:
            raise RuntimeError('官方 CDN 分段响应与请求范围不符，已停止安装。')
        time.sleep(2)
    raise RuntimeError('代理分段下载未完成；已完成的分段保留，重新打开即可继续。')


def download_archive(url, target):
    args=['curl','--fail','--silent','--show-error','--head','--location',
          '--proto','=https','--proto-redir','=https','--proxy',PROXY,'--noproxy','',
          '--connect-timeout','20','--max-time','40',url]
    head=subprocess.run(args,capture_output=True,text=True,timeout=50,env=environment())
    lengths=re.findall(r'^content-length:\s*(\d+)\s*$',head.stdout,re.I|re.M)
    if head.returncode or not lengths or not re.search(r'^accept-ranges:\s*bytes',head.stdout,re.I|re.M):
        return download(url,target,resume=True)
    total=int(lengths[-1])
    if not 0<total<=400*1024*1024:
        raise RuntimeError('官方安装包大小超出预期范围。')
    prefix=target.stat().st_size if target.exists() else 0
    if prefix>=total:
        target.unlink();prefix=0
    spans=ranges(prefix,total)
    chunks={start:target.with_name(target.name+f'.chunk-{start}-{end}') for start,end in spans}
    completed=prefix
    print(f'安装包 {total/1048576:.1f} MB；通过当前代理分 3 路下载，已保留 {prefix/1048576:.1f} MB。',flush=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures={pool.submit(download_range,url,chunks[start],start,end,total):(start,end) for start,end in spans}
        for future in as_completed(futures):
            future.result()
            start,end=futures[future];completed+=end-start+1
            print(f'下载进度 {completed/total:.0%} · {completed/1048576:.1f}/{total/1048576:.1f} MB',flush=True)
    try:
        with target.open('ab') as output:
            for start,end in spans:
                with chunks[start].open('rb') as source:
                    shutil.copyfileobj(source,output)
    except Exception:
        with target.open('r+b') as output:output.truncate(prefix)
        raise
    for path in chunks.values():
        path.unlink(missing_ok=True);path.with_name(path.name+'.json').unlink(missing_ok=True)


def installed():
    return (BASE / 'install/code').is_file() and (BASE / 'install/resources/app/product.json').is_file()


def install():
    if installed():
        print('VS Code 已准备，复用当前环境的安装与资料。', flush=True)
        return
    if platform.machine() not in ('x86_64', 'amd64'):
        raise RuntimeError('此版本的沙箱编辑器支持 Linux x64。')
    BASE.mkdir(parents=True, exist_ok=True)
    cache = BASE / 'downloads'
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='install-', dir=BASE) as tmp:
        folder = Path(tmp)
        print('下载官方 VS Code 更新清单（当前沙箱代理）…', flush=True)
        manifest = folder / 'release.json'
        download('https://update.code.visualstudio.com/api/update/linux-x64/stable/latest', manifest, 512 * 1024)
        info = metadata(json.loads(manifest.read_text()))
        print('下载 VS Code ' + info['productVersion'] + '，首次准备需要几分钟…', flush=True)
        archive = cache / (info['sha256hash'].lower() + '.tar.gz')
        # A previously completed download can be reused even after interrupted extraction.
        previous_hash = ''
        if archive.exists():
            with archive.open('rb') as source:
                previous_hash = hashlib.file_digest(source, 'sha256').hexdigest()
        if previous_hash != info['sha256hash'].lower():
            download_archive(info['url'], archive)
        with archive.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        if digest.lower() != info['sha256hash'].lower():
            archive.replace(archive.with_name(archive.name+'.rejected'))
            raise RuntimeError('安装包 SHA-256 与官方清单不符，已隔离该包，请重新打开以重新下载。校验值：'+digest)
        extract(archive, folder)
        candidate = folder / 'VSCode-linux-x64'
        if not (candidate / 'code').is_file() or not (candidate / 'resources/app/product.json').is_file():
            raise RuntimeError('安装包缺少 VS Code 主程序。')
        if (BASE / 'install').exists():
            raise RuntimeError('发现不完整的旧安装；保留现场，请检查沙箱编辑器安装目录。')
        candidate.rename(BASE / 'install')
        write_json(BASE / 'release.json', {'version':info['productVersion'], 'sha256':digest})
        archive.unlink(missing_ok=True)
        print('官方包校验通过，已安装到当前环境的持久化 home 卷。', flush=True)


def configure(label):
    from window_restore import install_binding
    install_binding()
    settings_file = BASE / 'user-data/User/settings.json'
    settings = json.loads(settings_file.read_text()) if settings_file.exists() else {}
    settings.update({
        'window.title': '${rootName}${separator}CCBOX · 沙箱 · ' + label + '${separator}${appName}',
        'http.proxy': PROXY, 'http.proxySupport':'override', 'http.proxyStrictSSL':True,
        'telemetry.telemetryLevel':'off', 'update.mode':'none', 'extensions.autoUpdate':False,
        'extensions.autoCheckUpdates':False, 'git.autofetch':False, 'remote.autoForwardPorts':False,
        'workbench.startupEditor':'none', 'window.restoreWindows':'one',
        'window.titleBarStyle':'custom', 'window.newWindowDimensions':'maximized',
        'terminal.integrated.defaultProfile.linux':'bash',
        'terminal.integrated.env.linux':{'BROWSER':'/usr/local/bin/ccbox-browser'},
    })
    write_json(settings_file, settings)
    keys_file = BASE / 'user-data/User/keybindings.json'
    keys = json.loads(keys_file.read_text()) if keys_file.exists() else []
    if not any(k.get('key') == 'f9' for k in keys):
        keys.append({'key':'f9', 'command':'claude-vscode.sidebar.open'})
    write_json(keys_file, keys)
    (BASE / 'extensions').mkdir(exist_ok=True)


def cli_args():
    # Docker's dropped capabilities + no-new-privileges disallow Chromium's inner
    # setuid sandbox. Only this Electron app uses --no-sandbox; Docker stays locked.
    return [str(BASE / 'install/bin/code'), '--no-sandbox',
            '--user-data-dir', str(BASE / 'user-data'), '--extensions-dir', str(BASE / 'extensions')]


def extension_installed():
    return any((p / 'package.json').is_file() for p in (BASE / 'extensions').glob(EXTENSION + '-*'))


def prepare_extension():
    if extension_installed():
        return
    # Reuse only extension binaries from THIS container, never host extension data.
    candidates = []
    for p in (Path.home() / '.vscode-server/extensions').glob(EXTENSION + '-*'):
        try:
            pkg = json.loads((p / 'package.json').read_text())
            if str(pkg.get('publisher','')).casefold() == 'anthropic' and pkg.get('name') == 'claude-code':
                candidates.append((tuple(int(v) for v in pkg['version'].split('.')), p))
        except (ValueError, KeyError, OSError):
            continue
    if candidates:
        source = max(candidates)[1]
        staging = BASE / 'extensions/.ccbox-installing'
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(source, staging)
        staging.rename(BASE / 'extensions' / source.name)
        print('已复用当前容器中的 Claude 扩展；没有复制宿主配置或凭据。', flush=True)
    else:
        print('通过当前代理安装官方 Claude 扩展…', flush=True)
        result = subprocess.run(cli_args() + ['--install-extension', EXTENSION],
                                env=environment(), capture_output=True, text=True, timeout=900)
        if result.returncode or not extension_installed():
            raise RuntimeError('Claude 扩展安装未完成，请检查当前代理后再次打开。' + result.stderr[-500:])


def classify_processes(rows):
    """Use executable/profile identity and ancestry; Chromium rewrites argv/env."""
    exe=str(BASE/'install/code')
    profile=str(BASE/'user-data')
    roots={pid for pid,row in rows.items() if row['exe']==exe and
           '--type=' not in row['raw'] and '--user-data-dir' in row['raw'] and
           profile in row['raw'] and '--extensions-dir' in row['raw']}
    descendants=set(roots)
    while True:
        children={pid for pid,row in rows.items() if row['ppid'] in descendants}
        if children <= descendants:
            break
        descendants |= children
    result={'desktop_main':bool(roots),'desktop_renderer':False,'desktop_extension_host':False,
            'desktop_claude_backend':False,'desktop_proxy_environment':False,'desktop_proxy_argument':False}
    for pid in descendants:
        row=rows[pid]
        raw,env=row['raw'],row.get('env',{})
        if pid in roots:
            result['desktop_proxy_environment'] |= env.get('HTTPS_PROXY')==PROXY
            result['desktop_proxy_argument'] |= '--proxy-server='+PROXY in raw
        if row['exe']==exe and '--type=renderer' in raw:
            result['desktop_renderer']=True
        if row['exe']==exe and ('--type=extensionHost' in raw or
                ('--type=utility' in raw and env.get('VSCODE_ESM_ENTRYPOINT')=='vs/workbench/api/node/extensionHostProcess')):
            result['desktop_extension_host']=True
        # A native language-server child inherits the marker but is not itself an extension host.
        if str(BASE/'extensions/anthropic.claude-code-') in row['exe'] and Path(row['exe']).name=='claude':
            result['desktop_claude_backend']=True
        # Chromium may replace its environment with its process title. An
        # extension-owned native child still identifies its direct Node host.
        if env.get('VSCODE_ESM_ENTRYPOINT')=='vs/workbench/api/node/extensionHostProcess':
            parent=rows.get(row['ppid'],{})
            if (row['ppid'] in descendants and parent.get('exe')==exe and
                    '--type=utility' in parent.get('raw','') and
                    '--utility-sub-type=node.mojom.NodeService' in parent.get('raw','')):
                result['desktop_extension_host']=True
    return result


def processes():
    rows={}
    for file in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            pid=int(file.parent.name)
            if pid==os.getpid():
                continue
            raw=file.read_bytes().replace(b'\0',b' ').decode(errors='replace')
            status=(file.parent/'status').read_text()
            ppid=int(next(line.split()[1] for line in status.splitlines() if line.startswith('PPid:')))
            # These few non-secret markers are all the inspector needs.
            env_raw=(file.parent/'environ').read_bytes().decode(errors='replace')
            env={}
            for key in ('HTTPS_PROXY','VSCODE_ESM_ENTRYPOINT'):
                match=re.search(r'(?:^|[\x00 ])'+key+r'=([^\x00 ]+)',env_raw)
                if match:
                    env[key]=match.group(1)
            rows[pid]={'exe':os.readlink(file.parent/'exe'),'ppid':ppid,'raw':raw,'env':env}
        except (OSError,ValueError,StopIteration):
            pass
    result=classify_processes(rows)
    result.update(installed=installed(),extension_installed=extension_installed())
    return result


def launch(workspace):
    if workspace not in ('/workspace', '/home/claude/.ccbox/workspaces/projects.code-workspace') and not re.fullmatch(r'/projects/p-[a-f0-9]{12}', workspace):
        raise ValueError('工作区路径应为当前环境生成的项目入口。')
    if not Path(workspace).exists():
        raise RuntimeError('项目入口尚未准备。')
    if not Path('/tmp/.X11-unix/X99').exists():
        raise RuntimeError('沙箱桌面尚未就绪，请稍后再次打开。')
    args = [str(BASE/'install/code'), '--no-sandbox', '--disable-gpu',
            '--user-data-dir', str(BASE/'user-data'), '--extensions-dir', str(BASE/'extensions'),
            '--proxy-server=' + PROXY, '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
            '--reuse-window', workspace]
    logfile = BASE / 'launch.log'
    if logfile.exists() and logfile.stat().st_size > 2*1024*1024:
        logfile.rename(BASE/'launch.previous.log')
    with logfile.open('ab') as output:
        subprocess.Popen(['dbus-run-session', '--', *args], env=environment(),
                         stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                         start_new_session=True, cwd='/workspace')
    for _ in range(35):
        time.sleep(1)
        status = processes()
        if status['desktop_main'] and status['desktop_renderer']:
            from window_restore import restore
            restore()
            print('VS Code 主程序和界面进程已在沙箱内启动。', flush=True)
            print(json.dumps(status), flush=True)
            return
    raise RuntimeError('未检测到沙箱编辑器界面进程。请查看容器 ~/.ccbox/sandbox-editor/launch.log 后重试。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['open','status'])
    parser.add_argument('--label', default='Workspace')
    parser.add_argument('--workspace', default='/workspace')
    args = parser.parse_args()
    if args.action == 'status':
        print(json.dumps(processes()))
        return
    import fcntl
    BASE.mkdir(parents=True, exist_ok=True)
    with (BASE/'prepare.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('当前环境的沙箱编辑器正在准备，请稍后重试。')
        install()
        configure(args.label)
        prepare_extension()
        launch(args.workspace)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('沙箱 VS Code：' + str(exc), flush=True)
        raise SystemExit(1)
