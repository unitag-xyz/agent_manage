# agent_manage Project Context

本仓库当前主要承载 `agent_manage` 这一层：运行在 OpenClaw 实例主机上的本机执行器，用来管理该主机内的 OpenClaw 实例与渠道配置。它不是网站前端，不是 Unipay 后台，也不是 AWS 控制台。

## 项目整体目标

这个项目服务于 Unipay 的 OpenClaw 管理体系，目标链路是：

- 模板作者提供自己的 OpenClaw 母版能力
- Unipay 负责商品销售、成交、交付和管理入口
- 买家支付后自动获得一个可使用的 `claw instance`
- 买家在 `My Claw` 页面管理自己的实例
- `agent_manage` 在实例主机内执行创建、绑定、恢复、升级等操作

## 4 层架构

### 1. 网站 / Unipay 层

- 安装位置：Unipay 前端
- 职责：商品销售、订单、支付成功后的 `My Claw` 页面、实例管理入口、微信/TG/控制台链接展示

### 2. 服务器管理层

- 安装位置：Unipay 后台
- 职责：AWS 服务器管理、镜像、自动建机、部署基础环境、注册服务器元信息

### 3. OpenClaw Server API 层

- 安装位置：claw 实例主机
- 职责：对外接受服务器控制命令，统一鉴权与审计，并调用本机 `agent_manage`

### 4. `agent_manage` 层

- 安装位置：claw 实例主机
- 职责：本机执行器，负责创建实例、绑定微信/TG、恢复配置、模板升级分析等
- 明确不负责：商品发布规则、模板市场权限判断、AWS 管理

## 核心对象

### `claw instance`

- 买家实际使用的运行实例
- 当前默认一个订单对应一个 `claw instance`
- 未来可扩展成“一个 claw 下有多个 agent”

MVP 实例页核心字段：

- 实例名称
- 实例有效期
- 实例描述
- 续费按钮
- 恢复按钮（恢复到上一次配置）
- 微信链接
- TG 链接
- 控制台链接

### `template project`

- 作者维护的母版工程
- 可以包含 soul、人设、规则、skills、程序、默认配置、资源
- 运行中的实例不能直接等同于可售母版

### `template release`

- 审核后、可版本化、可绑定商品的发布物
- 商品应绑定 `template release`，而不是绑定运行中的实例快照

## 当前 MVP 业务流

1. 用户在 Unipay 商品页下单
2. 支付成功后，系统先创建一个 `claw instance` 记录，状态为“开通中”
3. 后台异步创建 AWS 主机并部署 OpenClaw 环境
4. OpenClaw Server API 调用本机 `agent_manage` 初始化实例
5. 实例变为“可用”后，用户进入 `My Claw`

当前 My Claw 先做单实例详情页，不急于做列表页。

## `agent_manage` 当前/未来职责

重点能力：

- `create_instance`
  - 按商品绑定的母版创建新实例
  - 母版包含 soul、skills、默认配置等
  - 再按订单/用户信息覆盖必要字段
- `bind_wechat` / `unbind_wechat`
  - 只负责接收并写入微信 key，不负责前端二维码生成
  - 需要验证写入后是否需要 reload / restart
- `bind_tg` / `unbind_tg`
  - 接收 Bot Token 和必要参数，写入实例配置并建立绑定
- `restore_backup`
  - 当前偏向仅恢复实例配置，不恢复 workspace 内容
- 模板升级支持
  - 后续支持模板版本记录、升级分析、冲突处理

## 模板与升级原则

- 不建议直接从运行实例目录提取出“可售母版”
- 从实例提取更适合做内部草稿生成，而不是直接发布商品
- 模板应理解为“配置层 + 能力层”的组合发布物
- 建议将模板与 skills 分开管理：模板声明依赖，skills 单独版本化和审核

升级方面：

- 未来升级不能简单覆盖用户实例
- 建议分 3 层处理：
  - 母版基础层
  - 用户自定义层
  - 运行态数据层
- 未来需要关注的对象：
  - `template_release`
  - `instance_base_version`
  - `instance_overrides`
  - `upgrade_job`
- AI 更适合做升级差异分析和合并建议，不适合默认自动合并复杂程序逻辑

## 当前 MVP 规则

- MVP 不开放普通作者自助上传整包即发售
- MVP 先走平台人工代发布作者母版
- 买家买到的是使用权，不是母版源内容，也不是再发布权
- 默认禁止把别人的模板直接 fork 后再售卖

## 详细草案

更完整的产品讨论、待定项和展开说明见 `PRODUCT_NOTES.md`。
