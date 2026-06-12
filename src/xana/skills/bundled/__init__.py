"""Bundled skills: loop / schedule / skill_creator.

注册进独立的 InMemorySkillRegistry(boot 内的 bundled_skills),不挂在
harness 的 skill_registry 上 —— 全文只经 skill_load 工具按需取,
每轮 prompt 只注入「# 可用技能」名字+描述列表(SkillsListingContributor)。
"""
