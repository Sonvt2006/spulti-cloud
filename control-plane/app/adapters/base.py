class CloudAdapter:
    def __init__(self, config):
        self.config = config

    def create_instance(self, instance_config):
        raise NotImplementedError("This method should be implemented by subclasses.")

    def delete_instance(self, instance_ref):
        raise NotImplementedError("This method should be implemented by subclasses.")

    def get_instance(self, instance_ref):
        raise NotImplementedError("This method should be implemented by subclasses.")

    def list_instances(self):
        raise NotImplementedError("This method should be implemented by subclasses.")
