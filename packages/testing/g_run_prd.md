# PRD：额度中心首页展示拒绝原因

## 背景
用户申请提额被拒后，在额度中心首页看不到具体拒绝原因，只能联系客服询问，体验差、客服压力大。

## 需求
在额度中心首页接口（LimitCenterHomeResp）增加 `rejectReason` 字段，展示当前用户最近一次提额申请的拒绝原因（若有）。

## 范围
- 仅 hc-limit：LimitCenterServiceImpl / LimitCenterController / LimitCenterHomeResp
- 只读展示，不改变审批/授信逻辑

## 验收
- 有拒绝记录的用户，首页返回 rejectReason（非空）
- 无拒绝记录的用户，rejectReason 为空
- 不影响现有额度查询性能
