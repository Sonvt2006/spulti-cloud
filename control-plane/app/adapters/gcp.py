from base import CloudAdapter


class GCPAdapter(CloudAdapter):
    def __init__(self, config):
        super().__init__(config)
        # Initialize AWS-specific resources here

    def create_instance(self, instance_config):
        # Implement AWS-specific logic to create an instance
        pass

    def delete_instance(self, instance_ref):
        # Implement AWS-specific logic to delete an instance
        pass

    def get_instance(self, instance_ref):
        # Implement AWS-specific logic to get an instance
        pass

    def list_instances(self):
        # Implement AWS-specific logic to list instances
        pass
