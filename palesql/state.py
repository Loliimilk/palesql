from dataclasses import dataclass, field
from typing import Any


@dataclass
class CLIState:
    connection: Any = None
    cursor: Any = None
    connections: list = field(default_factory=list)
    active_connection: int | None = None
    sql_buffer: str = ""
    headers: bool = False
    mode: str = "list"
    nullvalue: str = ""
    changes: bool = False
    bail: bool = False
    echo: bool = False
    parameters: dict = field(default_factory=dict)
    timer: bool = False
    stats: bool = False
    scanstats: str = "off"
    progress_calls: int = 0
    trace_mode: str = "off"
    trace_file: Any = None
    log_mode: str = "off"
    log_file: Any = None
    eqp: str = "off"
    explain: str = "auto"
    expert_options: tuple | None = None
    column_separator: str | None = None
    row_separator: str | None = None
    column_widths: tuple = ()
    crlf: bool = False
    safe_mode: bool = False
    safe_nonce: str | None = None
    auth_enabled: bool = False
    session_state: Any = None
    www_pending: bool = False
    testcase_name: str | None = None
    testcase_output: Any = None
    history: list[str] = field(default_factory=list)
    main_prompt: str = "PaleSQL: "
    continue_prompt: str = "    ...: "
    failed: bool = False
    exit_requested: bool = False
    scripts: list = field(default_factory=list)
    source_context: tuple | None = None
    console: Any = None
    output_file: Any = None
    once_file: Any = None

    def clear_sql(self):
        self.sql_buffer = ""
        self.expert_options = None
