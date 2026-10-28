

- config.yml（sg）：发 21 次 CMS 调用，一次就把两个地域的实例全拿回来；顺带拉新加坡的实例清单
- config-meta.yml（hk）：一次 CMS 调用都不发，唯一职责是用 ALIYUN_REGION=cn-hongkong 去拉香港的实例清单
