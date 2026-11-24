"""The built-in toolkit as runner Tools, dispatched exactly as gemini_tools.handle_tool_call_v10 does.

Declarations still come from the feature modules and the session config is
still built by gemini_tools.extend_config (unchanged); this only says how each
declared name runs. Everything is looked up at call time, so tests' patches
and the env switches behave as before. A new toolkit adds its Tool here (see
features/harness/README.md, "Registering a tool").
"""
from features.harness.tools import BLOCKING, NON_BLOCKING, Tool


def tools():
    from features.conversational_ability import voice_choice, volume
    from features.eyes import tools as eye_tools
    from features.toolkits import audiobooks, experimental, gemini_tools, listen, look_closely, web_search
    from features.toolkits.everyday import tools as everyday
    from features.toolkits.family import tools as family
    from features.person_recognition import people

    def core(name):
        return lambda args: gemini_tools.execute(name, args)

    async def public(session, call):
        return await experimental.run_call(call)  # v10's route, with its own limit inside.

    def attach_player(session, loop):
        audiobooks.attach(session, loop)  # For pause/chapter/station notes.

    async def look(session, call):
        if not (gemini_tools.enabled() and look_closely.enabled()):
            return {"error": "Toolkit is disabled"}
        return await look_closely.run(session)

    result = []
    for declaration in gemini_tools.DECLARATIONS + everyday.DECLARATIONS + family.DECLARATIONS:
        name = declaration["name"]
        result.append(Tool(name, declaration, core(name),
                           before=attach_player if name == audiobooks.DECLARATION["name"] else None))
    result.append(Tool(listen.NAME, listen.DECLARATION, core(listen.NAME), before=attach_player))
    result.append(Tool(look_closely.DECLARATION["name"], look_closely.DECLARATION, run=look))
    result.append(Tool(volume.NAME, volume.DECLARATION, core(volume.NAME)))
    result.append(Tool(voice_choice.NAME, voice_choice.DECLARATION, core(voice_choice.NAME)))
    # Face and voice recognition (Orange Pi 4 Pro line). Registered whether or not recognition
    # is on, like v10's declared names; gemini_tools.execute answers for a disabled one.
    for declaration in people.DECLARATIONS:
        result.append(Tool(declaration["name"], declaration, core(declaration["name"])))
    result.append(Tool(web_search.NAME, web_search.DECLARATION, core(web_search.NAME)))
    # The eye command is non-blocking with a SILENT result unless BAYMAX_EYE_TOOL_SILENT=0 (v10's "quiet").
    result.append(Tool(eye_tools.NAME, eye_tools.DECLARATION, core(eye_tools.NAME),
                       behavior=lambda: NON_BLOCKING if eye_tools.silent() else BLOCKING, scheduling="SILENT"))
    known = {tool.name for tool in result}
    for declaration in experimental.DECLARATIONS:
        if declaration["name"] not in known:
            result.append(Tool(declaration["name"], declaration, run=public))
    return result
