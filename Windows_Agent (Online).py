import os
import subprocess
import re
import json
import warnings
from typing import TypedDict, List, Dict, Any, Optional, Literal


import speech_recognition as sr
import pyttsx3

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import tool
from langchain_huggingface import HuggingFaceEndpoint, ChatHuggingFace
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_tavily import TavilySearch, TavilyExtract

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from dotenv import load_dotenv

load_dotenv()

warnings.filterwarnings("ignore")

llm_endpoint = HuggingFaceEndpoint(
    repo_id="Qwen/Qwen3-235B-A22B-Instruct-2507",
    temperature=0.1,
    max_new_tokens=512,
    do_sample=False,
)

llm = ChatHuggingFace(
    llm=llm_endpoint
)

# STATE
class AgentState(TypedDict, total=False):

    user_input: str

    intent: Literal[
        "END",
        "COMMAND",
        "CONVERSATION"
    ]

    rag_results: str

    command: str

    verification_required: bool

    verification_response: str

    verification_decision: Literal[
        "execution",
        "cancel"
    ]

    execution_result: str

    response: str

    history: List[Any]


def speak(text: str):
    recognizer = sr.Recognizer()
    engine = pyttsx3.init()

    engine.setProperty("rate", 145)
    engine.setProperty("volume", 1)

    print("Agent TK:", text)

    engine.say(text)
    engine.runAndWait()


def listen() -> str:
    recognizer = sr.Recognizer()

    with sr.Microphone() as source:

        print("\nListening...")

        recognizer.adjust_for_ambient_noise(
            source,
            duration=0.5
        )

        audio = recognizer.listen(source)

    try:

        query = recognizer.recognize_google(audio)

        print("You:", query)

        return query.strip()

    except sr.UnknownValueError:

        speak("I could not understand that.")

        return ""

    except sr.RequestError:

        speak("Speech recognition service is unavailable.")

        return ""


def User_node(state: AgentState):
    # tkae userinput
    # desides wheather it is COMMAND, CONVERSATION or END

    user_input = listen()

    history = state.get("history", [])

    if not user_input:
        return {
            "user_input": "",
            "history": history,
            "intent": "CONVERSATION"
        }

    history.append(
        HumanMessage(content=user_input)
    )

    prompt = f"""
You are the routing controller for a Windows voice automation agent.

Classify the user's latest request into exactly one category.

END:
The user wants to stop, exit, quit, or terminate the agent.

COMMAND:
The user wants the agent to perform an operation on Windows,
applications, files, folders, settings, processes, browser,
terminal, or another system resource.

CONVERSATION:
The user is having normal conversation, asking a question,
greeting, or saying something that does not require a
Windows operating-system action.

USER REQUEST:
{user_input}

Return ONLY one word:

END
COMMAND
CONVERSATION
"""

    response = llm.invoke(prompt)

    result = response.content.strip().upper()

    if result == "END":
        intent = "END"

    elif result == "COMMAND":
        intent = "COMMAND"

    else:
        intent = "CONVERSATION"

    return {
        "user_input": user_input,
        "history": history,
        "intent": intent
    }

def route_request(
    state: AgentState
) -> Literal[
    "end",
    "command",
    "conversation"
]:

    intent = state.get("intent")

    if str.lower(intent) == str.lower("END"):
        return "end"

    if str.lower(intent) == str.lower("COMMAND"):
        return "command"

    return "conversation"


def conversation_node(state: AgentState):
    # use to convecte with user
    user_input = state.get("user_input", "")
    history = state.get("history", [])

    conversation_history = "\n".join(
        f"{'User' if isinstance(message, HumanMessage) else 'Agent TK'}: "
        f"{message.content}"
        for message in history
    )

    prompt = f"""
You are Agent.

Have a natural conversation with the user.

Conversation history:
{conversation_history}

Current user message:
{user_input}

Respond naturally and concisely.
Do not execute operating-system commands.
"""

    response = llm.invoke(prompt)

    answer = response.content.strip()

    history.append(
        AIMessage(content=answer)
    )

    speak(answer)

    return {
        "response": answer,
        "history": history
    }

def find_command_results(result):
    # function for rag_node
    command_results = []

    for item in result["results"]:
        text = item.get("raw_content", "")

        if any(keyword in text.lower() for keyword in [
            "command",
            "cmd",
            "command prompt",
            "powershell",
            "terminal",
            "run this",
            "type the following"
        ]):
            command_results.append({
                "title": item["title"],
                "url": item["url"],
                "content": text
            })

    return command_results

def rag_node(state: AgentState):
    # gets data from website
    user_input = state.get("user_input", "")
    tavily_search = TavilySearch(
        max_results=5,
        topic="general",
        search_depth="advanced"
    )

    result = tavily_search.invoke({
        "query": f"Windows cmd command for: {user_input}",
        "include_domains": [
            "learn.microsoft.com"
        ]
    })

    urls = [
        item["url"]
        for item in result.get("results", [])
    ]

    tavily_extract = TavilyExtract()
    extracted = tavily_extract.invoke({
        "urls": urls[:3]
    })

    extracted = find_command_results(extracted)

    joined = chr(10).join(
        str(result)
        for result in extracted
    )

    return {
        "rag_results": joined
    }

def command_node(state: AgentState):
    # gets command

    user_input = state.get("user_input", "")
    rag_results = state.get("rag_results", "")

    prompt = f"""
You are a Windows CMD command generator.

Your ONLY task is to convert the USER REQUEST into the ONE
Windows CMD command required to perform that request.

USER REQUEST:
{user_input}

RETRIEVED WINDOWS DOCUMENTATION:
{rag_results}

IMPORTANT:
Use the retrieved documentation as supporting information.
Use your own knowledge of Windows CMD when necessary.

OUTPUT CONTRACT:
Your response MUST contain exactly ONE executable Windows CMD command
and NOTHING ELSE.

The first character of your response must be the first character
of the command.

The last character of your response must be the last character
of the command.

NEVER output:
- explanations
- reasoning
- analysis
- <think> or </think>
- introductions
- conclusions
- "Command:"
- "The command is:"
- "Here is the command:"
- Markdown
- backticks
- code fences
- comments
- bullet points
- numbered lists
- multiple commands
- alternative commands
- descriptions of the command

DO NOT explain what the command does.

DO NOT mention the user's request.

DO NOT mention the documentation.

DO NOT surround the command with any punctuation.

Examples of VALID output:
start microsoft.windows.camera:
notepad.exe
calc.exe
taskkill /F /IM notepad.exe

Examples of INVALID output:
The command is: notepad.exe
`notepad.exe`
Here is the command:
notepad.exe
The command opens Notepad using notepad.exe.

Return ONLY the executable Windows CMD command.
"""

    response = llm.invoke(prompt)

    command = response.content.strip()

    # remove thinking blocks
    command = re.sub(
        r"<think>.*?</think>",
        "",
        command,
        flags=re.IGNORECASE | re.DOTALL
    ).strip()

    # remove code fences
    command = re.sub(
        r"^```(?:cmd|bat|batch|windows)?\s*",
        "",
        command,
        flags=re.IGNORECASE
    )

    command = re.sub(
        r"\s*```$",
        "",
        command
    ).strip()

    # extract content from backticks
    matches = re.findall(
        r"`([^`]+)`",
        command,
        flags=re.DOTALL
    )

    if matches:
        command = max(matches, key=len).strip()

    # remove common command prefixes
    command = re.sub(
        r"^(?:command|cmd|windows command)\s*:\s*",
        "",
        command,
        flags=re.IGNORECASE
    ).strip()

    command = re.sub(
        r"^(?:the\s+)?command\s+is\s*:\s*",
        "",
        command,
        flags=re.IGNORECASE
    ).strip()

    # remove surrounding quotes
    command = command.strip().strip('"').strip("'").strip()

    # if explanation remains, try to extract a command-like line
    lines = [
        line.strip()
        for line in command.splitlines()
        if line.strip()
    ]

    if len(lines) > 1:

        command_patterns = [
            r"^(?:start|cmd|powershell|taskkill|tasklist|ipconfig|ping|tracert|nslookup|netstat|systeminfo|shutdown|"
            r"explorer|notepad|calc|mspaint|control|reg|sc|net|whoami|where|dir|cd|copy|move|del|erase|mkdir|rmdir|"
            r"echo|type|find|findstr|set|cls|title|color|hostname|ver|winver|msconfig|services\.msc|devmgmt\.msc|"
            r"diskmgmt\.msc|eventvwr\.msc|taskmgr\.exe|cmd\.exe|powershell\.exe|"
            r"[A-Za-z0-9_.-]+\.exe)"
        ]

        for line in lines:
            if any(
                re.search(
                    pattern,
                    line,
                    flags=re.IGNORECASE
                )
                for pattern in command_patterns
            ):
                command = line
                break

    # remove accidental surrounding punctuation
    command = command.strip().strip("`").strip()

    # final cleanup
    command = re.sub(
        r"^\s*(?:command|answer|output)\s*:\s*",
        "",
        command,
        flags=re.IGNORECASE
    ).strip()

    return {
        "command": command
    }

def verification_router(state: AgentState):

    command = state.get("command", "")

    prompt = f"""
You are the safety controller for Agent.

Determine whether the following Windows command requires
human confirmation before execution.

COMMAND:
{command}

Require confirmation for operations that can:
- delete files or folders
- terminate processes
- modify system settings
- install or uninstall software
- modify permissions
- change security configuration
- shutdown or restart the system
- overwrite important data
- perform potentially destructive operations

Do NOT require confirmation for harmless operations such as:
- opening an application
- opening a website
- opening Settings
- opening Calculator
- opening Notepad
- opening File Explorer
- reading information

Return ONLY:

YES

or

NO
"""

    response = llm.invoke(prompt)

    result = response.content.strip().upper()

    if "YES" in result:
        return "verification"

    return "execution"


def verification_node(state: AgentState):
    # verifys execution or cancel
    command = state.get("command", "")

    speak(
        f"I need your confirmation before executing this command: "
        f"{command}. Do you want me to proceed?"
    )

    response = listen()

    prompt = f"""
Determine whether the user is confirming or rejecting execution
of the command.

COMMAND:
{command}

USER RESPONSE:
{response}

Return ONLY one word:

YES

or

NO

Rules:
- YES only if the user clearly confirms execution.
- NO if the user rejects, cancels, refuses, or says not to proceed.
- If the response is ambiguous, return NO.
- Consider the meaning of the complete response, not individual keywords.

Return ONLY one word:

YES

or

NO

"""

    result = llm.invoke(prompt).content.strip().upper()

    if str.lower(result) == str.lower("YES"):
        decision = "execution"
    else:
        decision = "cancel"

    return {
        "verification_response": response,
        "verification_decision": decision
    }

def execution_node(state: AgentState):
    # executes
    # gives litle responce
    command = state.get("command", "")

    print("\nExecuting:", command)

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30
        )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode == 0:
            execution_result = (
                stdout
                if stdout
                else "Operation completed successfully."
            )
        else:
            execution_result = (
                f"Command failed.\n"
                f"{stderr or stdout}"
            )

    except subprocess.TimeoutExpired:
        execution_result = "The command timed out."

    except Exception as e:
        execution_result = f"Execution error: {str(e)}"

    prompt = f"""
The following Windows CMD command was executed:

{command}

Execution result:

{execution_result}

Give the user a short natural-language response describing
whether the requested operation was successful.

Do not generate another command.
"""

    response = llm.invoke(prompt)

    answer = response.content.strip()

    history = state.get("history", [])

    history.append(
        AIMessage(content=answer)
    )

    speak(answer)

    return {
        "execution_result": execution_result,
        "response": answer,
        "history": history
    }

def cancel_node(state: AgentState):
    # just appends

    message = "Operation cancelled."

    history = state.get("history", [])

    history.append(
        AIMessage(content=message)
    )

    speak(message)

    return {
        "response": message,
        "history": history
    }


def end_node(state: AgentState):
    # bye
    speak("Goodbye.")

    return state

graph = StateGraph(AgentState)

# nodes
graph.add_node("user", User_node)
graph.add_node("conversation", conversation_node)
graph.add_node("rag", rag_node)
graph.add_node("command", command_node)
graph.add_node("verification", verification_node)
graph.add_node("execution", execution_node)
graph.add_node("cancel", cancel_node)
graph.add_node("end", end_node)

# start
graph.add_edge(START, "user")

# routing
graph.add_conditional_edges(
    "user",
    route_request,
    {
        "end": "end",
        "command": "rag",
        "conversation": "conversation"
    }
)

# conversation
graph.add_edge("conversation", "user")

# rag
graph.add_edge("rag", "command")

# command
graph.add_conditional_edges(
    "command",
    verification_router,
    {
        "verification": "verification",
        "execution": "execution"
    }
)

# verification
graph.add_conditional_edges(
    "verification",
    lambda state: state["verification_decision"],
    {
        "execution": "execution",
        "cancel": "cancel"
    }
)

# execution
graph.add_edge("execution", "user")

# cancel
graph.add_edge("cancel", "user")

# end
graph.add_edge("end", END)

# compile
agent = graph.compile()

print(agent.get_graph().draw_mermaid())

# history
initial_state: AgentState = {
    "history": []
}

# run
final_state = agent.invoke(initial_state)