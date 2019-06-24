package com.javaproject.demo.mapper;


import com.javaproject.demo.model.User;
import org.apache.ibatis.annotations.*;

@Mapper
public interface UserMapper {
    @Insert("insert into user (name,account_id,token,gmt_create,gmt_modified) values (#{name},#{accountId},#{token},#{gmtCreate},#{gmtModified})")
    void insert(User user);

    @Insert("insert into user (name,account_id,token,gmt_create,password) values (#{name},#{accountId},#{token},#{gmtCreate},#{password})")
    void insertuser(User user);

    @Select("select * from user where token = #{token}")
    User findByToken(@Param("token") String token);

    @Select("select * from user where id = #{id}")
    User findById(Integer creator);

    @Select("select * from user where ACCOUNT_ID=#{ACCOUNT_ID} and PASSWORD=#{PASSWORD}")
    User findByAccount_Id(String ACCOUNT_ID,String PASSWORD);
    @Select("select * from user where account_id = #{accountId}")
    User findByUserId(@Param("accountId") String accountId);

    @Update("update USER set name=#{name},token = #{token},gmt_modified = #{gmtModified} where id=#{id}")
    void update(User dbUser);
}
