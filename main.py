from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger # 使用 astrbot 提供的 logger 接口

class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    TMPL = '''
    <div style="font-size: 32px;">
    <h1 style="color: black">{{playername}}</h1>
    </div>
    '''

    # 注册指令的装饰器。指令名为 helloworld。注册成功后，发送 `/helloworld` 就会触发这个指令，并回复 `你好, {user_name}!`
    @filter.command("数据查询")
    def add(self, event: AstrMessageEvent, playsID: str):
        url = await self.html_render(TMPL, {"playername": playsID})
        yield event.plain_result()  

    async def terminate(self):
        '''可选择实现 terminate 函数，当插件被卸载/停用时会调用。'''