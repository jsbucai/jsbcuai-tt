# 新商品建议与真实编码创建

参考原流程 `04_程序/prepare_new_product_api_suggestions.py`、`generate_new_products_from_match_review.py` 及 `05_测试结果/new_product_rules.sqlite`。模式库是命名、分类、编号前缀和相邻规格参考，不是当前编号占用事实。

先找当前目录同产品族、标准、直径、等级、表面和材料的参考商品及相邻长度；商品名、规格、型号和分类按完整证据提建议。不要把不同材料、端型、牙距、表面或版本归为相同商品。

受控程序应先核对：

- 当前API完整目录的拟用编号，包括停用和删除状态；旧冻结库的“下一个编号”不能用于正式分配。
- 完整属性查重：原流程用名称/规格/型号/产地组合；再加入本次明确材料、标准和分类证据。相同商品已存在应使用原编码，不能为了录单创建重复项。
- 分类完整路径映射唯一、启用的 `type_id`；新商品API的 `ParTypeId` 是该分类TypeId，不是仓库ID或商品ptype_id。
- 前缀来自已核实同类商品及分类规则；前缀所有已有号最大值+1，数字宽度至少4并保留更宽格式。不要优先填历史空号。分配和预留必须由数据库事务完成，Codex不自行选择号码。
- 管理员确认冻结建议版本和参数。重试先核实商品，不确定写入不可重复发送；成功后查得编码唯一真实商品身份，再重跑销售匹配。

原模式库有 `new_product_source_products`、`product_code_sequences`、`product_category_prefix_rules`、`product_creation_patterns`、`product_spec_series`。模式及号段是冻结历史数据，需要与当前API主表重新核对。当前队列若未提供分类或新建商品查询结果，输出建议与问题即可，不能说已经创建或可直接提交。

正式受控程序为 `core.sales_new_product` 和独立队列 `core.sales_new_product_jobs`，网络操作只在 `tools/sales_new_product_worker.py`。当前仅从同族、同标准、同等级、同表面和明确材料的当前参考商品复制命名/型号模板，并改本次明确规格；模板不唯一时给管理员选择。参考品名含原尺寸、材料或产地不能明确核实时保持待核对。API的`Area`官方是产地，旧目录有混用，不得把所有产地当材质，或自行默认碳钢、不锈钢。

已核查官方契约：[新增商品 erp.ptype.add](http://d7safe.mygjp.com.cn:8020/web/#/erpapi/407)、[精确查询 erp.ptype.listbykey](http://d7safe.mygjp.com.cn:8020/web/#/erpapi/444)、[分类查询 erp.ptype.classlist](http://d7safe.mygjp.com.cn:8020/web/#/erpapi/936)。新增用单项`ptypes`列表，禁止`IniQty`。顶层`iserror=false`仍可能有`errorinfos`，不能视为全项成功。精确查询没有可靠SKU，必须再从完整`ptype.list`核实同`ptypeid`的唯一`skus`、单位为支，实际库存另外读取江苏优螺仓库。写入超时、部分错误或身份不明保留未知，不能重发；恢复只能核验原编码。

2026-10-07真实classlist返回`profileid='0'`行哨兵；权限仍由当前token账套和当前商品父分类身份绑定。商品`parptypeid`关联classlist的`id`，新建`ParTypeId`取唯一已启用分类`typeid`，不能混用。网络只读验收与模拟写入测试不等于真实新建已经完成；实际第一条新编码仍以管理员冻结确认后的结果核实为准。
