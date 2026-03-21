class Stream:
    def __init__(self, stream_url: str, stream_name: str):
        self.stream_url = stream_url
        self.stream_name = stream_name

    def get_url(self):
        return self.stream_url
    
    def get_name(self):
        return self.stream_name

