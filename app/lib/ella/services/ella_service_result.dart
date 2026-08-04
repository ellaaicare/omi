import 'package:omi/backend/http/client_api_failure.dart';

class EllaServiceResult<T> {
  const EllaServiceResult._({this.value, this.failure, required this.isSuccess});

  const EllaServiceResult.success([T? value]) : this._(value: value, isSuccess: true);

  const EllaServiceResult.failure(ClientApiFailure failure) : this._(failure: failure, isSuccess: false);

  final T? value;
  final ClientApiFailure? failure;
  final bool isSuccess;

  bool get isFailure => !isSuccess;
}
