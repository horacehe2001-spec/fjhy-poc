# -*- coding: utf-8 -*-
"""大模型接入层。

三块：
  presets.py  厂商预置的装载（config/llm_presets.yaml → llm_provider_preset）
  client.py   对厂商的实际调用（列型号 / 发一句话），**唯一出网的地方**
  （配置的增删改查在 app/api/sys_llm.py）

为什么把出网集中到 client.py 一个文件：
出网调用是这个系统里唯一会把业务数据交给第三方的动作。
散在各处就没法回答「哪些代码会把数据发出去」这个问题，
而那恰恰是客户安全评审第一个问的。
"""
