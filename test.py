from bot import get_thankness

while True:
    msg = input("")
    thankness = get_thankness(msg)
    print(str(thankness))
