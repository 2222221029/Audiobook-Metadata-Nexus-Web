# 声境元枢 AudioMeta Nexus

面向 NAS / Docker 的有声书元数据处理 Web UI。容器启动后，在浏览器打开端口即可填写配置、获取元数据、保存参数、加入队列、启动处理、查看进度与日志。

## 目录约定

- `/data`：挂载需要处理的有声书专辑目录。
- `/config/process_params.json`：Web UI 保存和读取的默认配置文件。

## 快速启动

```bash
docker compose build
docker compose up -d
```

如需保护 Web 页面，可在 `docker-compose.yml` 设置 `AUDIOMETA_WEB_TOKEN`，然后在页面工具栏点击“设置访问令牌”。

访问地址：

```text
http://NAS_IP:8787
```

本机测试：

```text
http://localhost:8787
```

## NAS Compose 示例

```yaml
services:
  audiometa-nexus:
    image: audiometa-nexus:local
    container_name: audiometa-nexus
    restart: unless-stopped
    ports:
      - "8787:8787"
    volumes:
      - /vol1/1000/docker-build/有声书元数据刮削工具_Docker版/docker/config:/config
      - /vol1/1000/downloads/有声书:/data
    environment:
      PROCESS_CONFIG: /config/process_params.json
      INPUT_FOLDER: /data
      WEB_HOST: 0.0.0.0
      WEB_PORT: 8787
    command: ["python", "docker_web.py"]
```

## 命令行模式

不使用 Web UI 时，可以直接运行批处理入口：

```bash
docker compose run --rm audiometa-nexus python docker_cli.py --config /config/process_params.json
```

## FFmpeg

镜像构建时会内置 Linux 版 `ffmpeg` 和 `ffprobe`，容器内可直接调用。NAS Docker 通常运行 Linux 容器，因此不需要放入 Windows 的 `ffmpeg.exe` / `ffprobe.exe`。

## 注意事项

- 声境元枢会直接修改 `/data` 中的音频标签，并可能按处理规则重命名专辑文件夹。
- 首次部署建议先用一个测试专辑验证输出结果，再批量处理正式目录。
