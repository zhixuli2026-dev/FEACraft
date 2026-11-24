# FF-J24 Web 客户端更新与回滚

发布端对 Vite 成品进行打包，签入通道、序号、版本、归档长度、SHA-256 和清单有效期。更新端用独立发布公钥校验签名，从同源下载成品，核对归档再暂存解包。校验启动页及引用资源后，以一次原子指针切换发布新版。Nginx 继续服务当前版本，上一版本资源仍可被打开的标签页请求。工作台发现服务器版本变化时显示刷新按钮。用户可以在当前操作结束后刷新。 [更新实现](../server/app/services/client_update.py) · [Nginx 静态路径](https://nginx.org/en/docs/http/ngx_http_core_module.html#root)

## 运行

在 `platform/server` 下执行。发布方保管加密 Ed25519 私钥，更新节点只配置发布公钥。稳定与 beta 通道各有单调递增的序号。清单和归档目录发布到同一个 HTTPS 源，先提供 `packages/` 中的成品，再替换 `channels/` 中的对应清单。

```bash
python -m app.services.offline_license_admin generate-key --private-key publisher-private.pem --public-key publisher.pem
python -m app.services.client_update publish --dist ../web/dist --feed /srv/vibeflow-release-feed --channel stable --sequence 1 --version 1.0.0 --private-key publisher-private.pem
python -m app.services.client_update check --root /srv/vibeflow-client --origin https://releases.example.org --public-key publisher.pem --channel stable
python -m app.services.client_update update --root /srv/vibeflow-client --origin https://releases.example.org --public-key publisher.pem --channel stable
python -m app.services.client_update rollback --root /srv/vibeflow-client --public-key publisher.pem
```

首次更新成功后，将 `VIBEFLOW_CLIENT_RELEASE_ROOT` 指向 Linux 主机的该目录，并使用生产 Compose 的 `docker-compose.client-updates.yml` 叠加配置。定时执行相同的 `update` 命令可自动检查和发布。回滚前重新核对已安装归档、签名和站点文件。回滚保留各通道的已见最高序号，旧清单无法触发自动降级。更新边界是 Web 静态客户端，API 与数据的版本沿现有部署迁移流程。

## 实际验证

- 独立 Linux Docker API、Nginx 和实际构建文件验证更新。篡改签名、过期清单、真实 HTTP 下载中断和实际归档字节损坏均保留当前版本。beta 激活后旧资源仍返回 200。回滚恢复稳定版，签名正确但序号较旧的 beta 清单被拒绝。日志保留，测试容器、网络和原生临时卷已清理。 [实际更新](../../tmp/ff-j24-client-updates-izi9ta0b/worker-result.json) · [清理](../../tmp/ff-j24-client-updates-izi9ta0b/client-update-result.json)
- Edge 实际加载稳定版、在同一打开页面看到 beta 提示、通过按钮刷新到 beta。回滚提示出现后刷新回稳定版。桌面和 390px 提示均可用，前端构建通过。真实发布 CLI 通过加密私钥签名和归档摘要比对，默认 Nginx 和 Compose 叠加配置通过。此阶段模型调用 0 次。 [浏览器](../../tmp/ff-j24-client-updates-izi9ta0b/browser-result.json) · [移动端](../../tmp/ff-j24-client-updates-izi9ta0b/update-390.png) · [发布 CLI](../../tmp/ff-j24-publisher-cli-fcq5i94r/publisher-result.json) · [配置核对](../../tmp/ff-j24-client-updates-izi9ta0b/default-and-overlay-config.json)
- 原版 `VibeFlowStudio.exe` 静态字节可定位到明确 Windows x64/stable 查询字符串，已记录对应二进制 SHA-256。原版自动触发、安装和重启顺序仍缺直接证据。现有运行服务保持本地版本，实际更新使用隔离的部署与公钥。 [原版静态证据](../../tmp/ff-j24-original-static-20261124.json)
