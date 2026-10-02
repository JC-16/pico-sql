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

## 4. 演进日志

- **Day 1（2026-10-02）**：lexer / parser / AST / 内存执行引擎 / REPL / 测试 / CI。
  已知妥协：数据不落盘、主键索引用 dict、无 JOIN / 聚合。
- Day 2（计划）：slotted page 存储引擎 + LRU 缓冲池。
- Day 3（计划）：B+ 树 + 火山模型执行器。
- Day 4（计划）：WAL + 崩溃恢复 + 性能基准。
