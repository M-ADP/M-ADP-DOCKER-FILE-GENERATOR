from abc import ABC, abstractmethod


class BaseUseCase(ABC):

    @abstractmethod
    async def __call__(self, *args, **kwargs):
        raise NotImplementedError
