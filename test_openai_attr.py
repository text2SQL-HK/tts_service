
from openai import OpenAI
try:
    client = OpenAI()
    print("Client created")
    try:
        getattr(client, "responses")
        print("client.responses exists")
    except AttributeError:
        print("client.responses does NOT exist")

    client.responses.create(model="gpt-5.2", input=[])
except Exception as e:
    print(f"Error: {e}")
