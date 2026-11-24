# FF-B06 CAD 场景版本与跨 API 同步

2026-11-19 已用两个真实 Windows API、共享 SQLite 和文件存储、真实 CadQuery 以及 Edge 验收。

## 场景协议

默认连接 `/ws/cadscene?case_id=<id>&token=<JWT>` 仍先收到 `ready`。客户端沿用 `scene.replace` 和 `scene.clear`，收到的广播增加持久 `version`。设置 `snapshot=true` 后，`ready` 后还会收到 `scene.snapshot`，包含当前版本及最新显示来源。普通原生场景通过 `scene_url` 指向同一算例的正式几何读取接口。实时场景附带已核验的对象列表与文件摘要。[WebSocket 实现](../server/app/routers/cadscene.py) · [快照与场景版本](../server/app/services/cadscene_state.py)

可用 `expected_version` 限定上一次观察到的场景版本。过期写入返回 `scene_version_conflict` 和 `current_version`。原有 `expected_revision`、`expected_sha256` 及显式空值表示文件不存在的约定继续适用。版本采用算例全局的持久事件顺序，场景变更之间可以有编号间隔。[写入条件](../server/app/services/case_write.py)

写入事务同时提交工作场景和包含版本、大小与 SHA-256 的持久事件。复制进程依据同一个 case 事件版本跟随当前文件，并复核对象与该版本的摘要。断线浏览器建立新连接后用快照恢复当前场景，忽略此前已经见过的版本。未保存的历史传输状态由当前快照恢复，原生 CadQuery 历史发布始终可通过其 publication ID 读回。[浏览器订阅](../web/src/utils/cadSceneFeed.ts)

## 实际验收

两个独立 API 使用同一隔离 SQLite 和文件存储，分别连接旧协议和带快照的协议。原生倒角件经实际 CadQuery 作业发布。第一个浏览器快照在版本 0 指向该算例的原生场景。随后两边收到相同的实时替换、清空、恢复，依次为 36、41、46。第一次实时替换携带 513,499 字节的场景。两个 API 最后回读的当前场景字节一致，与版本 46 的持久摘要相同。[首个跨 API 更新](../../tmp/geometry-cadscene-replica-nzhjjai8/peer-scene-live.png) · [三次实际提交](../../tmp/geometry-cadscene-replica-nzhjjai8/cadscene-replica-result.json)

Edge 在版本 36 后实际断开了连接，期间错过版本 41 的清空。重新建立连接后，真实快照恢复了空画面。向该连接重复发送先前真实捕获的版本 36 后，浏览器未重新请求场景。随后版本 46 的新场景正常显示。版本、文件摘要、caseDict 修订冲突以及非法面索引均被拒绝。无权账户连接第二个 API 收到 HTTP 403。[清空快照](../../tmp/geometry-cadscene-replica-nzhjjai8/clear-restored-from-snapshot.png) · [最终画面](../../tmp/geometry-cadscene-replica-nzhjjai8/recovered-scene.png) · [浏览器回执](../../tmp/geometry-cadscene-replica-nzhjjai8/cadscene-replica-browser.json)

两个 API 的历史 CadQuery 场景摘要与原发布一致，当前可变场景按草稿读取。两台独立 API 和原生工作进程均核验退出，Windows 作业内的六个进程全部退出。页面错误、真实模型和模拟模型调用均为 0。有意断联时 Windows 网络层记录连接重置，重新连接与后续读取通过。[隔离退出](../../tmp/geometry-cadscene-replica-nzhjjai8/cleanup-independent.json) · [版本过滤断言](../../tmp/geometry-cadscene-replica-nzhjjai8/cadscene-feed-primitives.log)

首轮的双 API 传播及冲突检查已经通过，随后夹具错误地等待了本机 Playwright 关闭包装器的空返回值。修正夹具为通过浏览器侧连接关闭后，使用同一正式构建完整复验。[首轮回执](../../tmp/geometry-cadscene-replica-aby18h07/cadscene-replica-browser.json) · [正式入口](../server/geometry_cadscene_replica_acceptance.py) · [66 项精确源审查](../../tmp/FF-B06-cadscene-20261119/source-review-v2.json)
