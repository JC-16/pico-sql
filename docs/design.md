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

### 3.10 语句级原子性：两阶段执行（严格审查后确立）

INSERT 与 UPDATE 都按"阶段一全量校验、阶段二统一写入"执行：

1. **阶段一（纯函数，零写入）**：逐行求值 → `_coerce` 类型/范围校验
   （INT 必须落在 INT64，否则编码层会炸出裸 struct.error）→
   `encode_row` 试编码（暴露超长 VARCHAR / 溢出）→ 主键查重
   （对现有索引 + 对同语句内已暂存行）。
2. **阶段二（不可失败）**：所有行已验证可编码，逐行写入。

效果：**任何一条语句要么完整生效、要么零残留**——`INSERT INTO t
VALUES (1,'a'),(1,'b')` 报主键冲突后，(1,'a') 也不存在。

边界（诚实）：这是**语句级**原子性，不是事务级——两条语句之间没有原子边界，
且崩溃时阶段二写到一半仍可能留下半条语句（缓冲池脏页语义）。Day 4 的 WAL
把保证从"语句失败零残留"升级为"崩溃后零残留"。DELETE 同理按"先匹配后删除"
执行，但因删除本身无编码失败路径，不需要试编码。

### 3.11 B+ 树：分隔符取"下界"约定（Day 3）

本实现采用最小度数形式：`t = order`，MAX_KEYS = 2t-1，MIN_KEYS = t-1
（非根），节点满 2t 键即分裂；叶溢出**复制**中间键上推（分隔符 = 右半首键），
内部溢出**移动**中间键上推。

最重要的约定：**分隔符是右子树的下界**（左子树键 < sep ≤ 右子树键），
不要求严格等于右子树最小键。这个选择让删除简单得多：删除某键后，
等于该键的分隔符仍是合法下界（子树最小值只会变大），无需向上修复；
只有借用/合并引起的边界变化需要调整相邻分隔符。若采用"分隔符恰等于
右子树首键"的严格约定，删除叶首键后必须沿路径修复所有等值分隔符——
两种都对，我们选了少写一类 bug 的那种。

叶子节点带 `next` 指针串成链表：范围扫描从定位叶开始沿链平推，
不再逐层下降（这正是 B+ 树优于 B 树的标志性设计）。

删除实现完整：优先向兄弟**借用**（叶改父分隔符为新的首键），
借不到则**合并**（父分隔符下坠），下溢沿路径向上传播，
内部根节点键数归零时坍缩为孩子（树高 -1）。
正确性由 4000 次随机操作与 dict 参考实现的对拍测试保证
（`tests/test_btree.py::test_randomized_differential_against_reference`）。

### 3.12 火山模型执行器（Day 3）

每个算子实现 `open()/next()/close()`，数据被上层**逐行拉取**：

```
Limit -> Project -> Sort -> Filter -> Index/SeqScan
```

- **惰性**：Limit 拉满 N 行就停止调用下层 next——测试用计数算子证明
  100 行表 LIMIT 5 只拉了 5 行；
- **Sort 是阻塞算子**：open 时必须排空子算子（真实引擎对超内存输入用
  外部归并排序，v1 诚实标注）；
- DML 暂以批处理形态存在，未表达为算子（Known Limitations）。

火山模型的价值：让"流水线"成为统一的组合方式，每个算子独立可测；
代价：每行一次虚函数调用的解释开销——真实引擎用向量化/编译执行对抗，
这是 Day 4 基准图能直观展示的话题。

### 3.13 索引选择规则（v1 的"优化器"）

只做一条规则，但完整、可解释：把 WHERE 拍平成 AND 因子，凡形如
`主键 OP 字面量`（或镜像 `字面量 OP 主键`）的因子即提取为索引边界——
等值 → 树点查；范围 → 叶子链区间扫描。其余因子（OR 子树、列间比较、
非主键列）保留为 FilterOperator 的残差。

已知边界：OR 无法用单个范围表达（真实优化器做索引合并/多范围）；
每表仅主键一个索引（真实引擎支持多个二级索引）；ORDER BY 不走索引
（留作优化课题）；混合类型边界（str vs int）判定为不可用退回全扫。
`db.last_scan_used_index` 暴露规划结果供测试与基准。

## 4. 演进日志

- **Day 1（2026-10-02）**：lexer / parser / AST / 内存执行引擎 / REPL / 测试 / CI。
  已知妥协：数据不落盘、主键索引用 dict、无 JOIN / 聚合。
- **Day 2（2026-10-03）**：storage 子包（pages / record / bufferpool / heap /
  catalog）+ 引擎改造为 row_id 语义 + 页回收（DROP 的页复用）+ 持久化闭环测试。
  新增妥协：崩溃语义 pre-WAL（close 前崩溃丢脏页）、页 0 JSON catalog、
  无 pin/unpin（单线程不需要）。
- **严格审查（2026-10-03，Day 2 后）**：修复 4 项——①`_coerce` 增加 INT64
  范围检查（此前溢出值会以裸 struct.error 逃逸出引擎边界）；②INSERT 改两阶段
  执行（此前多行插入中途失败会留下部分行）；③UPDATE 阶段一增加试编码（此前
  超长值在提交期才失败，造成部分提交）；④parser 容忍空语句/连续分号
  （对齐 SQLite/PostgreSQL 行为）。新增 7 个回归测试固化以上行为；
  Known Limitations 补 4 条已知偏差（PK 允许 NULL、无文件锁、码点序比较、
  无科学计数法）。
- **Day 3（2026-10-03）**：B+ 树索引（storage/btree.py，分裂/合并/借用完整实现
  + 随机对拍）替换 dict 主键索引；executor.py 火山模型算子组；SELECT 接入
  火山流水线与主键索引下推（点查/范围/残差过滤），`last_scan_used_index`
  暴露规划结果；HeapTable 新增 fetch(row_id)。新增妥协：仅主键单列索引、
  OR/列间比较不走索引、Sort 阻塞无外排、DML 未算子化。
- **第二轮加固（2026-10-03，对抗审查第二遍，覆盖第一轮未触及的区域）**：
  修复 6 项——①REPL 按 `;` 切分语句时不感知字符串字面量，含分号的多行
  字符串会被切碎且用户可能被卡死（修复：引号奇偶校验 + `.quit` 永远可用，
  字符串内的 `.quit` 行视为内容）；②目录页 JSON 损坏时裸 json.JSONDecodeError
  逃逸出引擎边界（修复：收敛为 PageError，打开数据库时转为 EngineError）；
  ③FilePageFile 打开失败（权限/路径/被锁）泄漏裸 OSError（修复：包为
  PageError）；④Database.close() 二次调用会因关闭的文件句柄抛裸 ValueError
  （修复：close 幂等）；⑤close 后再 execute 泄漏裸 ValueError（修复：池与
  引擎增加 closed 状态，拒绝操作）；⑥词法器"数字开头的标识符"行为写入文档
  并用测试固化。新增 10 个回归测试（124 total）。
- Day 4（计划）：WAL + 崩溃恢复 + 性能基准。
