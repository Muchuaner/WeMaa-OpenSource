"""Use the existing verified contact route for sparse customer greetings."""
import json,time
from experiment_client import ExperimentClient

class GreetingClient(ExperimentClient):
    def __init__(self,root,store,replies):
        super().__init__(root,store,replies)
        self.path=self.root/'data'/'greetings-control.json'
        self.control={'enabled':False,'items':[]};self.greeting=True
    def poll(self,reports):
        ack=[]
        for event,attempts,reason in reports:
            try:
                response=self.replies.transport('/greetings/navigation',{'event_id':event,'attempts':attempts,'reason':reason},'POST')
                if response.get('accepted'):ack.append(event)
            except Exception:continue
        result=self.replies.transport('/greetings/plan',{},'GET');result['_navigation_acks']=ack;return result
    def applies_to(self,body):return False
    def process_incoming_one(self,runtime,run_id):return False
    def queue_incoming(self,body,snapshot,ordinal,target,runtime,image,payload):
        return self.replies.queue(body,snapshot,ordinal,target)
