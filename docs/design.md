# pico-sql 设计文档

本文档回答三个问题：每一层为什么存在、为什么这样设计、真实数据库怎么做。

## 1. 总体管线

```
SQL 文本
  │  lexer.tokenize()      字符流 → Token 流（带行列号）
  ▼
Token 流
  │  parser.parse()        递归下降 → AST（frozen dataclass）
  ▼
AST
  │  Database.execute()    遍历 AST 执行
  ▼
结果（QueryResult / ExecuteResult）
```

每层只依赖下一层的输出，测试可以逐层进行：
lexer 的测试不碰 AST，parser 的测试不碰执行，engine 的测试直接从 SQL 字符串进。

## 2. 文法（v1 子集，EBNF）

```ebnf
script        = statement { ";" statement } [ ";" ] ;
statement     = create_table | drop_table | insert | select | update | delete ;

create_table  = "CREATE" "TABLE" identifier "(" column_def { "," column_def } ")" ;
column_def    = identifier type [ "PRIMARY" "KEY" ] ;
type          = "INT" | "FLOAT" | "VARCHAR" [ "(" number ")" ] | "BOOL" ;

drop_table    = "DROP" "TABLE" identifier ;

insert        = "INSERT" "INTO" identifier
                [ "(" identifier { "," identifier } ")" ]
                "VALUES" value_row { "," value_row } ;
value_row     = "(" expression { "," expression } ")" ;

select        = "SELECT" ( "*" | identifier { "," identifier } )
                "FROM" identifier
                [ "WHERE" expression ]
                [ "ORDER" "BY" identifier [ "ASC" | "DESC" ] ]
                [ "LIMIT" number ] ;

update        = "UPDATE" identifier "SET" assignment { "," assignment } [ "WHERE" expression ] ;
assignment    = identifier "=" expression ;
delete        = "DELETE" "FROM" identifier [ "WHERE" expression ] ;

(* precedence: OR < AND < NOT < comparison < additive < multiplicative < unary < primary *)
expression    = or_expr ;
or_expr       = and_expr { "OR" and_expr } ;
and_expr      = not_expr { "AND" not_expr } ;
not_expr      = "NOT" not_expr | comparison ;
comparison    = additive [ ( "=" | "!=" | "<>" | "<" | "<=" | ">" | ">=" ) additive ] ;
additive      = multiplicative { ( "+" | "-" ) multiplicative } ;
multiplicative= unary { ( "*" | "/" | "%" ) unary } ;
unary         = "-" unary | primary ;
primary       = number | string | "NULL" | "TRUE" | "FALSE"
              | identifier | "(" expression ")" ;
```

## 3. 关键设计决策

### 3.1 为什么手写解析器而不用 ANTLR / sqlite3？

项目的目的是理解每一层。用现成解析器等于把最值得学的一层外包出去。
递归下降是生产级数据库（SQLite 的 LEMON、PostgreSQL 的手写解析器）同样采用的主流路线，
规模可控时它比解析器生成器更可读、报错更友好。

### 3.2 AST 为什么用 frozen dataclass？

- `frozen=True` 保证 AST 不可变——解析产物不应被执行过程污染；
- `dataclass` 自动生成 `__eq__`，测试可以直接用 `==` 对比整棵表达式树；
- 节点无行为，执行逻辑集中在 engine：改执行策略不用动解析器。

### 3.3 表达式为什么按优先级分层？

每层一个方法、只处理自己级别的运算符并向下委托：
`parse_or` 处理 OR，其操作数交给 `parse_and`……
这样优先级由"调用深度"天然表达，不需要优先级表，
新增一个级别 = 新增一个方法 + 改一行调用链。

### 3.4 主键索引：dict 起步，B+ 树接班（Day 3）

v1 用 `dict[value -> row]` 只支持点查。
B+ 树相对 dict 的真实优势是**范围扫描 + 有序遍历 + 面向磁盘的页粒度**，
这正是 Day 3 要实现的部分。诚实的演进路径比一步到位更有教学价值。

### 3.5 三值逻辑

`NULL` 参与比较结果是 `NULL`（UNKNOWN），`WHERE` 只保留 TRUE 的行。
所以 `v = 10 OR v <> 10` 不会匹配 `v IS NULL` 的行——这是 SQL 的经典陷阱，
实现一遍比背十遍有效。

### 3.6 Slotted Page：为什么两端生长、为什么墓碑、为什么压缩保号

**两端生长**：槽目录从页头向后长，记录从页尾向前长。如果记录和目录同向生长，
插入记录就要挪动已有记录或目录——两端生长让"插入一条记录"只改变
`data_start` 一个字段，已有记录永不移动。

**墓碑（tombstone）**：删除记录时把槽位标记为 (0,0) 而不是移动后续记录——
因为外部持有 `row_id = (page_id, slot_no)`，移动即破坏引用。代价是页内碎片。

**页内压缩保号**：碎片太多时，把存活记录连续重写到页尾，但**槽位号保持不变**
（墓碑仍是墓碑，位置留原地）。于是 row_id 在压缩前后依然有效。
真实引擎的等价物是页内空间整理 + slot 编号稳定性（InnoDB 等各有取舍）。

**记录编码**：每列 1 字节 NULL 标志 + 载荷（INT 8B / FLOAT 8B / BOOL 1B /
VARCHAR 变长 2B 长度前缀 + UTF-8）。NULL 标志占 1 字节/列——真实引擎用
NULL 位图（每列 1 bit），这是 Day 2 的动手练习。

### 3.7 缓冲池 vs 操作系统 page cache

OS 已经有 page cache，为什么数据库还要自己做缓冲池？
1. **知识**：数据库知道页的访问模式（索引叶子比 catalog 更热）和写语义
   （脏页必须按 WAL 顺序落盘——Day 4），OS 不知道；
2. **控制**：逐出策略（LRU/时钟）、脏页回写时机、预读都要为 B+ 树扫描优化；
3. **一致性**：数据库要保证自己的 fsync 边界，绕开 OS 的回写不确定性。

实现上 v1 的 LRU 用 OrderedDict；命中/未命中计数暴露给 Day 4 基准；
真实引擎还有 pin/unpin 防止"正在使用的页被逐出"——v1 是单线程同步执行，
操作期间页不会在半途被逐，故省略（多线程化时必须补上）。

### 3.8 row_id 契约（执行器与存储层的边界）

- `row_id = (page_id, slot_no)`，槽位号永久稳定（压缩保号）；
- `update()` 就地更新返回原 row_id；放不下时"墓碑 + 重插"，返回**可能不同**
  的 row_id，调用方必须采用返回值（主键索引就是这么做的）；
- `row_id` 是临时的逻辑坐标：重建数据库后同一条记录的 row_id 不同——
  它不是行身份，主键才是。

### 3.9 Catalog 为什么放页 0、为什么是 JSON

页 0 = JSON（schema + 每表的页链 + 空闲页链）。好处：数据路径完全走页，
仓库里的数据库文件可以 `xxd` 直接看懂；代价：无事务性目录更新、单页 4KB
上限。真实做法：PostgreSQL 的 pg_class / SQLite 的 sqlite_master，
存于专用二进制页并参与崩溃恢复。

## 4. 演进日志

- **Day 1（2026-10-02）**：lexer / parser / AST / 内存执行引擎 / REPL / 测试 / CI。
  已知妥协：数据不落盘、主键索引用 dict、无 JOIN / 聚合。
- **Day 2（2026-10-03）**：storage 子包（pages / record / bufferpool / heap /
  catalog）+ 引擎改造为 row_id 语义 + 页回收（DROP 的页复用）+ 持久化闭环测试。
  新增妥协：崩溃语义 pre-WAL（close 前崩溃丢脏页）、页 0 JSON catalog、
  无 pin/unpin（单线程不需要）。
- Day 3（计划）：B+ 树 + 火山模型执行器。
- Day 4（计划）：WAL + 崩溃恢复 + 性能基准。
