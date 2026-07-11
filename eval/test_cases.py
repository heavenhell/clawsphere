TEST_CASES = [
    {
        "name": "alarm_list",
        "message": "我现在有哪些告警？",
        "intent": "alert_explain",
        "must_tools": ["list_alarms"],
        "must_contain": ["活动告警", "alarm-9001"],
    },
    {
        "name": "alarm_second_context",
        "message": "第二条呢",
        "history": [
            {"role": "assistant", "content": "当前共有 2 条活动告警：alarm-9001，alarm-9002"}
        ],
        "intent": "alert_explain",
        "must_contain": ["alarm-9002", "CPU"],
    },
    {
        "name": "resource_vm_count",
        "message": "有多少虚拟机",
        "intent": "resource_query",
        "must_tools": ["get_resource_overview"],
        "must_contain": ["5 台虚拟机"],
    },
    {
        "name": "capacity_forecast",
        "message": "cluster-002 还能撑多久？",
        "intent": "capacity_forecast",
        "must_tools": ["run_capacity_forecast"],
        "must_contain": ["容量风险"],
    },
    {
        "name": "write_denied",
        "message": "帮我重启 dcs-app-01",
        "intent": "change_execute",
        "must_contain": ["护栏", "readonly"],
    },
]
